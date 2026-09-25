# -*- coding: utf-8 -*-
"""
comfy_engine.py —— 引擎生命周期管理（启动 / 停止 / 互斥 / 真实状态检测）

设计要点（对应用户明确要求）：
  1. **一律手动启动**：模块只提供能力，绝不自动拉起任何引擎。
  2. **互斥**：任一时刻只允许一个引擎在跑（开 LLM 自动关 ComfyUI，反之亦然）。
     参数 exclusive="all" 全局互斥；"group" 仅绘图组 vs 对话组互斥。
  3. **真实检测**：不只看端口，而是「进程 + 端口 + HTTP 探针 + 显存 + 模型加载状态」四层，
     并且能识别「不是本程序启动的」（外部双击 bat 起的）实例。
  4. **显存仲裁**：16GB 显存带不动两个引擎，启动前按预估占用校验，不足则拒绝（force 可越过）。
  5. **停止要彻底**：优雅中断 → taskkill /T 杀整棵进程树 → 等端口释放 → 等显存回落。

零第三方依赖：只标准库 + 系统 nvidia-smi / netstat / taskkill。
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

# 显存释放判定：占用回落到该值以下视为已释放（桌面本身约 1~2GB）
VRAM_RELEASED_GB = 4.0


# --------------------------------------------------------------------------- #
# 默认引擎定义（全部来自本机实测路径；界面上可改，存回 config.json）
# --------------------------------------------------------------------------- #
def default_engines():
    return {
        "comfy": {
            "name": "ComfyUI 绘图",
            "group": "draw",
            "enabled": True,
            "exe": r"X:\你的ComfyUI\venv\Scripts\python.exe",
            "args": ["main.py", "--listen", "127.0.0.1", "--port", "8188"],
            "cwd": r"X:\你的ComfyUI",
            "port": 8188,
            "probe_url": "http://127.0.0.1:8188/system_stats",
            "interrupt_url": "http://127.0.0.1:8188/interrupt",
            "vram_est_gb": 12.0,
            "ready_timeout": 240,
            "hint": "首次启动需 30-90 秒；如失败请检查 exe/cwd 是否与实际一致",
        },
        # ⚠ 曾经这里还有一个 "h3" 引擎（指向一个第三方整合包里的 API 服务 @8288）。2026-09-23 按用户要求**彻底移除**：
        #   H3 视频改为与 ComfyUI 绘图共用同一个 8188 实例（ComfyUI 原生 MiniMax-H3
        #   节点 + CreateVideo/SaveVideo），这样换台机器只要有 ComfyUI 就能跑，
        #   不用再指望别人也装了那个整合包自带的 API 服务。
        #   注意 EngineManager 是「默认值 + 配置覆盖」，配置**删不掉**这里的键，
        #   所以必须从默认值里删干净，否则界面上会一直挂着一个连不上的幽灵引擎。
        "llm": {
            "name": "LLM 文字生成",
            "group": "chat",
            "enabled": True,
            "exe": r"X:\你的llama.cpp\llama-server.exe",
            # 单模型模式（推荐）：--model <gguf> --alias <名字>，起完就能对话。
            # 想同时挂多个模型把 router 设成 true（需保证 presets.ini 段里带 model=）。
            "args": [],
            "auto_args": True,
            "router": False,
            "cwd": r"X:\你的llama.cpp",
            "host": "0.0.0.0",
            "port": 8080,
            "probe_url": "http://127.0.0.1:8080/v1/models",
            "vram_est_gb": 13.0,
            "ready_timeout": 420,
            "hint": "参数按显卡自动算；模型库 X:\\你的llama.cpp\\models（目录联接会被自动穿透）",
            "models_dir": r"X:\你的llama.cpp\models",
            "presets": r"X:\你的llama.cpp\presets.ini",
            "model": r"X:\你的llama.cpp\models\Qwen3.8-27B-OrcaRouter-GSQ-RCO-IQ3_XXS-v2.0.gguf",
            # alias 不再设短名：build_llm_args 会回退为「模型文件名去 .gguf」作为对话名，
            # 2026-09-23 用户要求界面显示完整模型名（新/旧量化版同叫 Qwen3.8-27B，短名分不清）。
            "ctx": 65536,
            "max_predict": 8192,
            # 2026-09-23 用户硬性要求：LLM 强制常开思考模式。
            # 当前仅用 OrcaRouter-GSQ-RCO 新量化版，官方要求 thinking ON 才有预期推理行为。
            "thinking": "on",
        },
        "ollama": {
            "name": "Ollama（可选）",
            "group": "chat",
            "enabled": False,
            "exe": r"C:\你的Ollama目录\ollama.exe",
            "args": ["serve"],
            "cwd": r"C:\你的Ollama目录",
            "port": 11434,
            "probe_url": "http://127.0.0.1:11434/api/tags",
            "vram_est_gb": 6.0,
            "ready_timeout": 120,
            "hint": "已有模型 qwen3vl-uncensored（多模态，可图片问答）",
        },
    }


# --------------------------------------------------------------------------- #
# 系统查询小工具
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=15, **kw):
    # ⚠️ 必须带 CREATE_NO_WINDOW：本程序是 windowed（无控制台）的 exe，
    # 不带这个标志时 Windows 会给每个子进程（nvidia-smi / netstat / tasklist /
    # taskkill）新开一个控制台窗口，界面每隔几秒轮询就会闪一次黑框。
    # 实测表现为「CMD 窗口一直打开又闪退」。
    kw.setdefault("creationflags", CREATE_NO_WINDOW)
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, **kw)
        return (p.stdout or b"").decode("utf-8", "replace"), (p.stderr or b"").decode("utf-8", "replace"), p.returncode
    except Exception as e:
        return "", str(e), -1


# 短 TTL 缓存：netstat / nvidia-smi 都是几百毫秒到 1 秒级的子进程，
# 一次 status_all(deep) 或 detect 会被调用好几次，不缓存会明显卡界面。
_TTL_CACHE = {}
_TTL_LOCK = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _TTL_LOCK:
        hit = _TTL_CACHE.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    val = fn()
    with _TTL_LOCK:
        _TTL_CACHE[key] = (time.time(), val)
    return val


def gpu_info(ttl=2.0):
    """返回 {name, total, used, free}（GB）。没有 nvidia-smi 时返回 None。

    ttl=0 表示强制实时读取（显存仲裁等需要准数的场合必须用 0）。
    """
    def _read():
        out, _, rc = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free",
                           "--format=csv,noheader,nounits"], timeout=8)
        if rc != 0 or not out.strip():
            return None
        line = out.strip().splitlines()[0]
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 4:
            return None
        try:
            return {"name": parts[0],
                    "total": round(float(parts[1]) / 1024, 1),
                    "used": round(float(parts[2]) / 1024, 1),
                    "free": round(float(parts[3]) / 1024, 1)}
        except Exception:
            return None

    return _read() if not ttl else _cached("gpu", ttl, _read)


def _cache_clear(prefix=None):
    """清缓存。prefix=None 清全部；给了前缀只清对应键。"""
    with _TTL_LOCK:
        if prefix is None:
            _TTL_CACHE.clear()
            return
        for k in [k for k in list(_TTL_CACHE) if str(k).startswith(prefix)]:
            _TTL_CACHE.pop(k, None)


def is_admin_hint():
    return "管理员"


def _netstat_listen(ttl=2.5):
    """缓存一份 TCP LISTENING 快照，返回 [(本地地址, PID), ...] 或 None。

    一次「检测全部引擎」要查 3 个端口，每个端口单跑一次 netstat 会白等 1~2 秒，
    所以按快照缓存。ttl=0 强制实时（杀进程后等端口释放必须用 0）。
    """
    def _read():
        out, _, rc = _run(["netstat", "-ano", "-p", "TCP"], timeout=12)
        if rc != 0:
            return None
        rows = []
        for ln in out.splitlines():
            if "LISTENING" not in ln.upper():
                continue
            t = ln.split()
            if len(t) >= 2 and t[-1].isdigit():
                rows.append((t[1], int(t[-1])))
        return rows

    return _read() if not ttl else _cached("netstat", ttl, _read)


def _pid_on_port(port, ttl=2.5):
    """找出监听该端口的 PID（能发现外部启动的实例）。ttl=0 强制实时。"""
    rows = _netstat_listen(ttl=ttl)
    if rows is None:
        return None
    needle = ":%d" % int(port)
    for addr, pid in rows:
        if addr.endswith(needle):
            return pid
    return None


def _proc_alive(pid):
    if not pid:
        return False
    out, _, _ = _run(["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"], timeout=10)
    return str(pid) in out


def _kill_tree(pid, timeout=20):
    if not pid:
        return False
    _run(["taskkill", "/PID", str(int(pid)), "/T", "/F"], timeout=timeout)
    return True


def _http_ok(url, timeout=5):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Comfy-ThirdParty-Frontend/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 400
    except Exception:
        return False


def _http_json(url, timeout=8):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Comfy-ThirdParty-Frontend/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else {}
    except Exception as e:
        return {"_error": str(e)}


# --------------------------------------------------------------------------- #
# llama 启动参数：移植 llama-oneclick.ps1 的「探 --help → 按显存档位算参」逻辑
# --------------------------------------------------------------------------- #
_HELP_CACHE = {}


def _llama_help(exe):
    if exe in _HELP_CACHE:
        return _HELP_CACHE[exe]
    out, err, _ = _run([exe, "--help"], timeout=25)
    txt = (out or "") + (err or "")
    _HELP_CACHE[exe] = txt
    return txt


def _known_models_path(root):
    return os.path.join(root, ".known_models.json")


def _load_known_models(root):
    """读取已知模型清单（含已移走的），用于「文件不在了也保留灰态」。"""
    p = _known_models_path(root)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("models"), list):
            return data["models"]
    except Exception:
        pass
    return []


def _save_known_models(root, models):
    p = _known_models_path(root)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"models": models}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def scan_models(d):
    """扫描 LLM 引擎的模型库目录（os.walk 可穿透 mklink /J 目录联接），返回 gguf 列表。

    跳过 mmproj / 投影模型（视觉塔，不能单独当对话模型跑）。
    每个模型带 exists 字段：文件被移到别处（如 NAS 机械盘）时为 False，UI 直接灰掉。
    已知清单持久化：本次扫到的新模型名写入 .known_models.json；
    清单里存在但本次目录里没扫到的，也会补回并标 exists=false（保留灰态，不消失）。
    """
    root = d.get("models_dir")
    out = []
    if not root or not os.path.isdir(root):
        return out
    active = os.path.normcase(os.path.abspath(d.get("model") or "")) if d.get("model") else ""
    seen = set()
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                low = fn.lower()
                if not low.endswith(".gguf") or "mmproj" in low or "projector" in low:
                    continue
                fp = os.path.join(dirpath, fn)
                exists = os.path.isfile(fp)
                try:
                    sz = os.path.getsize(fp) if exists else 0
                except Exception:
                    sz = 0
                name = os.path.splitext(fn)[0]
                try:
                    rel = os.path.relpath(dirpath, root)
                except Exception:
                    rel = "."
                key = os.path.normcase(os.path.abspath(fp))
                seen.add(key)
                out.append({
                    "name": name,
                    "path": fp,
                    "size_gb": round(sz / float(1 << 30), 2),
                    "subdir": "" if rel in (".", "") else rel,
                    "active": bool(active) and key == active,
                    "exists": exists,
                })
    except Exception:
        pass

    # 已知清单补回：曾经扫到、现在目录里没了（被移走）的模型 → 灰态保留
    try:
        known = _load_known_models(root)
        for k in known:
            kp = k.get("path") if isinstance(k, dict) else k
            if not kp:
                continue
            kkey = os.path.normcase(os.path.abspath(kp))
            if kkey in seen:
                continue
            if not os.path.isfile(kp):
                out.append({
                    "name": os.path.splitext(os.path.basename(kp))[0],
                    "path": kp,
                    "size_gb": 0,
                    "subdir": "",
                    "active": bool(active) and kkey == active,
                    "exists": False,
                })
    except Exception:
        pass

    # 持久化本次存在的模型（更新清单，供下次补回）
    try:
        cur = [{"path": o["path"], "name": o["name"]} for o in out if o.get("exists")]
        _save_known_models(root, cur)
    except Exception:
        pass

    out.sort(key=lambda x: (-(1 if x["exists"] else 0), -x["size_gb"], x["name"]))
    return out


def llm_active_model(d):
    """取实际要加载的模型路径：配置优先，否则取模型库里最大的 gguf。"""
    mp = d.get("model") or ""
    if mp and os.path.isfile(mp):
        return mp
    cands = scan_models(d)
    return cands[0]["path"] if cands else ""


def llm_vram_est(d):
    """按模型文件大小估显存占用（权重 + KV/上下文/计算缓冲余量）。"""
    mp = llm_active_model(d)
    if mp and os.path.isfile(mp):
        size_gb = os.path.getsize(mp) / float(1 << 30)
        return round(size_gb * 1.05 + 1.5, 1)
    try:
        return float(d.get("vram_est_gb") or 12.0)
    except Exception:
        return 12.0


def build_llm_args(d):
    """按显卡自动生成 llama-server 参数。返回 (args, notes)；(None, [原因]) 表示无法拼装。

    两种模式：
      * 单模型（默认，d["router"] 为假）→ --model <gguf> --alias <名字>，起完即可对话。
      * router（d["router"] 为真）→ --models-dir + --models-preset，多模型按需加载。
        ⚠️ 本机实测：自定义 preset 段若只写 ctx-size 不写 model，会**覆盖掉**自动发现的
        模型路径，导致 router 拉起的子进程没有 -m、永久卡在 "waiting until model is
        fully loaded"。所以 router 模式下必须保证 preset 里带 model，否则别开。
    """
    exe = d.get("exe") or ""
    help_txt = _llama_help(exe) if os.path.isfile(exe) else ""
    notes = []

    def has(flag):
        return flag in help_txt

    g = gpu_info()
    vram = g["free"] if g else 0.0
    total = g["total"] if g else 0.0
    # 用总显存定档（free 会因桌面占用偏低），与 oneclick 档位保持一致
    tier_vram = total
    if tier_vram >= 32:
        ctx, kvq, max_models, tier = 32768, False, 2, "32G+"
    elif tier_vram >= 24:
        ctx, kvq, max_models, tier = 32768, True, 1, "24G"
    elif tier_vram >= 16:
        ctx, kvq, max_models, tier = 32768, True, 1, "16G"
    elif tier_vram >= 11:
        ctx, kvq, max_models, tier = 16384, True, 1, "12G"
    elif tier_vram >= 7:
        ctx, kvq, max_models, tier = 8192, True, 1, "8G"
    else:
        ctx, kvq, max_models, tier = 4096, True, 1, "CPU/小显存"

    ctx = int(d.get("ctx") or ctx)
    n_predict = int(d.get("max_predict") or 8192)

    # Flash Attention 语法探测
    fa_mode = "none"
    if has("--flash-attn"):
        fa_mode = "value" if re.search(r"flash-attn.*\[on\|off\|auto\]", help_txt) else "flag"

    args = ["--host", str(d.get("host") or "0.0.0.0"), "--port", str(int(d.get("port") or 8080))]
    if d.get("router"):
        if d.get("models_dir"):
            args += ["--models-dir", d["models_dir"]]
        if d.get("presets") and os.path.isfile(d["presets"]) and has("--models-preset"):
            args += ["--models-preset", d["presets"]]
            notes.append("router 模式：preset 段必须带 model=，否则子进程无 -m 会永久挂起")
    else:
        mp = llm_active_model(d)
        if not mp:
            return None, ["未找到可用的 gguf 模型：请检查 models_dir（%s）或指定 model 路径"
                          % (d.get("models_dir") or "(未配置)")]
        args += ["--model", mp]
        alias = d.get("alias") or os.path.splitext(os.path.basename(mp))[0]
        if has("--alias"):
            args += ["--alias", alias]
        # 视觉投影器自动挂载：模型库里存在配套 mmproj-*.gguf 时挂上 → 模型可看图。
        # 2026-09-23：用户下载了 mmproj-Qwen3.8-27B-BF16.gguf，此前没挂导致 A 只能纯文本。
        # 匹配策略：优先同名投影器，其次目录里任意 mmproj-*.gguf。
        if has("--mmproj"):
            _mmdir = d.get("models_dir") or os.path.dirname(mp)
            _mm = ""
            try:
                if os.path.isdir(_mmdir):
                    _cands = [f for f in os.listdir(_mmdir)
                              if f.lower().startswith("mmproj") and f.lower().endswith(".gguf")]
                    if _cands:
                        # 优先与主模型同系列名（含 27B / Qwen3.8）的
                        _cands.sort(key=lambda f: (0 if ("27b" in f.lower() or "qwen3.8" in f.lower()) else 1, f))
                        _mm = os.path.join(_mmdir, _cands[0])
            except Exception:
                _mm = ""
            if _mm and os.path.isfile(_mm):
                args += ["--mmproj", _mm]
                notes.append("视觉已启用：%s（可看图，多占约 %.1f GB 显存）"
                             % (os.path.basename(_mm), os.path.getsize(_mm) / float(1 << 30)))
            else:
                notes.append("未找到 mmproj 投影器 → 纯文本模式（放 mmproj-*.gguf 到模型库可开启看图）")
        try:
            size_gb = os.path.getsize(mp) / float(1 << 30)
        except Exception:
            size_gb = 0.0
        notes.append("单模型模式：%s（%.2f GB，对话名 %s）"
                     % (os.path.basename(mp), size_gb, alias))
    args += ["-t", str(max(4, min((os.cpu_count() or 8), 16)))]
    args += ["-c", str(ctx)]
    if d.get("router") and has("--models-max"):
        args += ["--models-max", str(max_models)]
    # 生成上限：不给上限时模型复读会吃满上下文直至卡死
    if has("--predict"):
        args += ["-n", str(n_predict)]
    elif has("--n-predict"):
        args += ["--n-predict", str(n_predict)]
    if fa_mode == "value":
        args += ["-fa", "on"]
    elif fa_mode == "flag":
        args += ["-fa"]
    if kvq and has("--cache-type-k"):
        args += ["--cache-type-k", "q8_0"]
        if fa_mode != "none" and has("--cache-type-v"):
            args += ["--cache-type-v", "q8_0"]
    if has("--jinja"):
        args += ["--jinja"]
    # 关闭深度思考（Qwen3 系默认会输出 <thinking>，新手最容易被它截断）。
    # ⚠ 实测（2026-09-23，Qwen3.8-27B-Uncensored-IQ2_M）：单给 --reasoning-budget 0
    #   对这类社区模板**不生效** —— 模型照样把整段推理流进 delta.reasoning_content，
    #   max_tokens 被推理吃光后 delta.content 一直是 null，前端气泡全空。
    #   所以两个开关都下：budget 0（标准模板生效）+ chat-template-kwargs（Qwen 模板生效）。
    #   想要思考过程：引擎配置 "thinking": "on"（见下）。
    # ⚠ 2026-09-23 用户硬性要求：LLM 强制常开思考，不看模型名。
    #   当前仅用 OrcaRouter-GSQ-RCO 新量化版，官方要求 thinking ON 才有预期推理行为；
    #   前端开关同步灰掉不可改（见 comfy_studio.html updateChatEngineUI 的 thinking_locked）。
    _mp_now = llm_active_model(d)
    _locked = bool(_mp_now) and "orcarouter-gsq-rco" in os.path.basename(_mp_now).lower()
    # thinking_locked：锁定态（前端灰掉）仍按模型名判定；think_on 则无条件常开
    think_on = True
    if has("--reasoning-budget"):
        args += ["--reasoning-budget", "-1" if think_on else "0"]
    if has("--chat-template-kwargs"):
        args += ["--chat-template-kwargs",
                 '{"enable_thinking":%s}' % ("true" if think_on else "false")]
    # 投机解码（MTP）：模型自带 nextn 头（本机 gguf 里有 blk.64.nextn.*），
    # 不需要另挂 draft 模型文件 → 直接开 --spec-type draft-mtp 就能加速。
    # 2026-09-23 实测：开 vs 不开做同一题对比（见文档 §3.2）。
    spec = str(d.get("spec") or "").strip()
    if spec and has("--spec-type"):
        args += ["--spec-type", spec]
        if d.get("spec_draft_n_max"):
            args += ["--spec-draft-n-max", str(int(d["spec_draft_n_max"]))]
        notes.append("投机解码已启用：--spec-type %s（用模型内置 MTP 头，n-max=%s）"
                     % (spec, d.get("spec_draft_n_max") or "默认3"))
    # 采样默认值（非思考模式）
    for flag, val in (("--temp", "0.7"), ("--top-p", "0.8"), ("--top-k", "20"),
                      ("--min-p", "0"), ("--presence-penalty", "1.0")):
        if has(flag):
            args += [flag, val]
    # 显存分配
    # 2026-09-23：实测 --fit-target 自动放置会把 layer 0 丢到 CPU（日志：
    #   "layer 0 is assigned to device CPU but the fused Gated Delta Net tensor is
    #    assigned to device CUDA0"）→ 每步 CPU↔GPU 同步，生成速度掉到 7.8 tok/s。
    # 显存明明还有 2GB 没用。所以允许配置显式指定 ngl，走"全量上 GPU"。
    if d.get("ngl"):
        args += ["-ngl", str(int(d["ngl"]))]
        notes.append("显式 -ngl %s（跳过 --fit 自动放置，避免层被丢到 CPU）" % int(d["ngl"]))
    elif has("--fit-target"):
        args += ["--fit-target", "2048" if tier_vram >= 16 else "1024"]
    elif g:
        args += ["-ngl", "99"]
        notes.append("本版本无 --fit，已回退 -ngl 99 全量 offload")
    notes.insert(0, "档位 %s（显存 %sGB，可用 %sGB）/ ctx=%d / 生成上限 %d"
                 % (tier, total or "?", (g or {}).get("free", "?"), ctx, n_predict))
    if not help_txt:
        notes.append("未能读取 llama-server --help，参数按保守默认值拼装")
    return args, notes


# --------------------------------------------------------------------------- #
# 引擎管理器
# --------------------------------------------------------------------------- #
class EngineManager(object):
    def __init__(self, engines_cfg=None, log_dir=None, exclusive="all", autostart=False):
        self._lock = threading.RLock()
        self.log_dir = log_dir or os.path.join(HERE, "logs")
        try:
            os.makedirs(self.log_dir, exist_ok=True)
        except Exception:
            self.log_dir = os.environ.get("TEMP") or "."
        base = default_engines()
        # 与配置合并（配置优先级更高）
        for k, v in (engines_cfg or {}).items():
            if k in base and isinstance(v, dict):
                base[k].update(v)
            elif isinstance(v, dict):
                base[k] = v
        self.engines = base
        self.exclusive = (exclusive or "all").lower()
        self._procs = {}       # eid -> {proc, pid, started_at, log_path, external:False}
        self._states = {}      # eid -> off|starting|ready|stopping|error
        self._errors = {}
        self._notes = {}
        self.autostart = autostart

    # ---------------- 基础 ----------------
    def defn(self, eid):
        return self.engines.get(eid) or {}

    def enabled_ids(self):
        return [k for k, v in self.engines.items() if v.get("enabled", True)]

    def _log_path(self, eid):
        return os.path.join(self.log_dir, "engine-%s.log" % eid)

    def _rotate(self, path, max_mb=8):
        try:
            if os.path.isfile(path) and os.path.getsize(path) > max_mb * 1024 * 1024:
                bak = path + ".1"
                if os.path.isfile(bak):
                    os.remove(bak)
                os.replace(path, bak)
        except Exception:
            pass

    # ---------------- 状态 ----------------
    def is_running(self, eid):
        """进程活 OR 端口在听，都算在跑（能识别外部启动的实例）。"""
        with self._lock:
            info = self._procs.get(eid)
            if info and not info.get("external"):
                p = info.get("proc")
                if p is not None and p.poll() is None:
                    return True
                if info.get("pid") and _proc_alive(info["pid"]):
                    return True
        d = self.defn(eid)
        return _pid_on_port(d.get("port")) is not None

    def running_ids(self):
        return [eid for eid in self.engines if self.is_running(eid)]

    def state(self, eid):
        with self._lock:
            st = self._states.get(eid)
        if st in ("starting", "stopping", "error"):
            return st
        return "ready" if self.is_running(eid) else "off"

    def owner(self, eid):
        with self._lock:
            info = self._procs.get(eid)
        if info:
            return "app"
        return "external" if self.is_running(eid) else "none"

    def status(self, eid, deep=False):
        d = self.defn(eid)
        run = self.is_running(eid)
        with self._lock:
            info = dict(self._procs.get(eid) or {})
        pid = info.get("pid") or (_pid_on_port(d.get("port")) if run else None)
        out = {
            "id": eid,
            "name": d.get("name") or eid,
            "group": d.get("group") or "other",
            "exe": d.get("exe"),
            "cwd": d.get("cwd"),
            "port": d.get("port"),
            "vram_est_gb": d.get("vram_est_gb"),
            "hint": d.get("hint"),
            "enabled": d.get("enabled", True),
            "running": bool(run),
            "pid": pid,
            "owner": self.owner(eid),
            "state": self.state(eid),
            "started_at": info.get("started_at"),
            "uptime": int(time.time() - info["started_at"]) if info.get("started_at") else None,
            "log": self._log_path(eid),
            "error": self._errors.get(eid) or "",
            "notes": self._notes.get(eid) or [],
        }
        if run or deep:
            out["port_open"] = _pid_on_port(d.get("port")) is not None
            out["http_ok"] = _http_ok(d.get("probe_url"))
            out["ready"] = bool(out["http_ok"])
            if eid == "llm" and out["http_ok"]:
                j = _http_json("http://127.0.0.1:%d/v1/models" % int(d.get("port") or 8080))
                out["models"] = [m.get("id") for m in (j.get("data") or []) if m.get("id")]
            elif eid == "ollama" and out["http_ok"]:
                j = _http_json("http://127.0.0.1:%d/api/tags" % int(d.get("port") or 11434))
                out["models"] = [m.get("name") for m in (j.get("models") or []) if m.get("name")]
        return out

    def status_all(self, deep=False):
        g = gpu_info()
        return {
            "engines": [self.status(eid, deep=deep) for eid in self.engines],
            "running": self.running_ids(),
            "exclusive": self.exclusive,
            "gpu": g,
        }

    def log_tail(self, eid, n=120):
        p = self._log_path(eid)
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 64 * 1024))
                raw = f.read().decode("utf-8", "replace")
            lines = raw.splitlines()[-int(n):]
            return {"ok": True, "path": p, "lines": lines}
        except Exception as e:
            return {"ok": False, "path": p, "lines": [], "error": str(e)}

    # ---------------- 检测（给「检测按钮」用） ----------------
    def detect(self, eid):
        d = self.defn(eid)
        port = d.get("port")
        port_pid = _pid_on_port(port)
        with self._lock:
            info = dict(self._procs.get(eid) or {})
        own_pid = info.get("pid") or None
        # 优先认「本程序拉起的那个 pid」：ComfyUI 启动要 30-90 秒才监听端口，
        # 只看 netstat 会在这段时间里误报「未运行」。
        pid = port_pid or own_pid
        rep = {
            "id": eid, "name": d.get("name") or eid, "port": port,
            "exe_exists": os.path.isfile(d.get("exe") or ""),
            "cwd_exists": os.path.isdir(d.get("cwd") or ""),
            "port_pid": port_pid, "own_pid": own_pid,
            "proc_alive": _proc_alive(pid) if pid else False,
            "http_ok": _http_ok(d.get("probe_url")),
            "owner": self.owner(eid),
            "state": self.state(eid),
            "vram": gpu_info(),
        }
        rep["ok"] = bool(rep["proc_alive"] and rep["http_ok"])
        if rep["ok"]:
            rep["verdict"] = "正在运行（进程 %s，端口 %s 探针正常）" % (pid, port)
        elif rep["proc_alive"]:
            rep["verdict"] = "进程在（PID %s）但探针无响应 —— 可能仍在加载模型/启动中" % pid
        elif port_pid:
            rep["verdict"] = "端口 %s 有进程（PID %s）但探针无响应" % (port, port_pid)
        elif rep["http_ok"]:
            rep["verdict"] = "探针正常但未在本机端口找到进程 —— 可能跑在别处/另有隧道"
        else:
            rep["verdict"] = "未运行"
        if not rep["exe_exists"]:
            rep["verdict"] += "；⚠ 可执行文件不存在：%s" % d.get("exe")
        if eid == "llm" and rep["http_ok"]:
            j = _http_json("http://127.0.0.1:%d/v1/models" % int(port or 8080))
            mods, loaded = [], None
            for m in (j.get("data") or []) + (j.get("models") or []):
                mid = m.get("id") or m.get("name") or m.get("model")
                if not mid or any(x["id"] == mid for x in mods):
                    continue
                st = m.get("status")
                if isinstance(st, dict):
                    st = st.get("value")
                meta = m.get("meta") or {}
                mods.append({"id": mid, "status": st or ""})
                if meta.get("n_ctx"):
                    loaded = {"id": mid, "n_ctx": meta.get("n_ctx"),
                              "n_params": meta.get("n_params"), "size": meta.get("size")}
            rep["models"] = mods
            _amp = llm_active_model(d)
            rep["active_model"] = _amp
            rep["model_missing"] = bool(_amp) and not os.path.isfile(_amp)
            rep["thinking_locked"] = bool(_amp) and "orcarouter-gsq-rco" in os.path.basename(_amp).lower()
            if loaded:
                rep["loaded"] = loaded
                rep["verdict"] = ("正在运行（进程 %s，端口 %s，模型 %s 已驻留，ctx=%s）"
                                  % (pid, port, loaded["id"], loaded["n_ctx"]))
            elif mods:
                rep["verdict"] = ("router 已就绪但模型尚未加载（%s）—— 首次对话会先加载，可能等数分钟"
                                  % "、".join(x["id"] for x in mods))
            else:
                rep["verdict"] = "llama-server 在跑，但没列出任何模型 —— 参数可能有问题，看日志"
        return rep

    # ---------------- 启动 ----------------
    # 启动指定引擎：先做显存仲裁（不足且非 force 则拒），再按配置 exe/cwd/args
    # 起子进程；起完后用「进程+端口+HTTP探针」四层真实检测确认就绪。
    def start(self, eid, force=False):
        with self._lock:
            d = self.defn(eid)
            if not d:
                return {"ok": False, "error": "未知引擎 %s" % eid}
            if self.is_running(eid):
                return {"ok": True, "already": True, "pid": self.status(eid).get("pid"),
                        "msg": "%s 已在运行" % d.get("name")}

            # 1) 互斥：先停其它引擎
            stopped = []
            for other in self.engines:
                if other == eid or not self.is_running(other):
                    continue
                if self.exclusive == "all" or self.defn(other).get("group") != d.get("group"):
                    self._stop_locked(other)
                    stopped.append(other)

            # 2) 显存校验（必须实时值，缓存会误判）
            g = gpu_info(ttl=0)
            if eid == "llm":
                est = llm_vram_est(d)
            else:
                est = float(d.get("vram_est_gb") or 0)
            if g and not force and est and g["free"] < est * 0.6:
                return {"ok": False, "code": "VRAM",
                        "error": "显存不足：当前空闲 %.1fGB，%s 约需 %.1fGB。请先停止其它引擎或勾选「仍然启动」。"
                                 % (g["free"], d.get("name"), est),
                        "gpu": g, "stopped": stopped}

            # 3) 拼命令
            args = list(d.get("args") or [])
            notes = []
            if d.get("auto_args") or (not args):
                if eid == "llm":
                    args, notes = build_llm_args(d)
            if args is None:
                return {"ok": False, "code": "NOMODEL", "error": "；".join(notes) or "参数拼装失败",
                        "stopped": stopped}
            cmd = [d.get("exe")] + [str(a) for a in args]
            if not os.path.isfile(d.get("exe") or ""):
                return {"ok": False, "code": "NOEXE", "error": "可执行文件不存在：%s" % d.get("exe")}

            # 4) 起进程
            lp = self._log_path(eid)
            self._rotate(lp)
            try:
                f = open(lp, "ab", buffering=0)
                f.write(("\n\n===== %s 启动 %s =====\n%s\n" %
                         (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd),
                          "\n".join(notes))).encode("utf-8", "replace"))
                p = subprocess.Popen(cmd, cwd=d.get("cwd") or None, stdin=subprocess.DEVNULL,
                                     stdout=f, stderr=subprocess.STDOUT, creationflags=_FLAGS)
            except Exception as e:
                self._errors[eid] = str(e)
                self._states[eid] = "error"
                return {"ok": False, "code": "SPAWN", "error": "启动失败：%s" % e}

            self._procs[eid] = {"proc": p, "pid": p.pid, "started_at": time.time(),
                                "log_path": lp, "cmd": cmd, "external": False}
            self._states[eid] = "starting"
            self._errors.pop(eid, None)
            self._notes[eid] = notes

        threading.Thread(target=self._wait_ready, args=(eid,), daemon=True).start()
        return {"ok": True, "pid": p.pid, "cmd": cmd, "notes": notes, "stopped": stopped}

    def _wait_ready(self, eid):
        d = self.defn(eid)
        url = d.get("probe_url")
        deadline = time.time() + float(d.get("ready_timeout") or 180)
        while time.time() < deadline:
            with self._lock:
                info = self._procs.get(eid)
                st = self._states.get(eid)
            # 被互斥踢掉 / 用户手动停掉：不是失败，直接收工（否则会误弹「启动失败」）
            if st in ("stopping", "off") or (info is None and st != "starting"):
                return
            if info:
                pr = info.get("proc")
                if pr is not None and pr.poll() is not None:
                    # 进程自己退出了：读日志尾部给出原因
                    tail = self.log_tail(eid, 15).get("lines") or []
                    with self._lock:
                        already_off = self._states.get(eid) in ("stopping", "off")
                    if already_off:
                        return
                    self._states[eid] = "error"
                    self._errors[eid] = "进程已退出（返回码 %s）。日志尾部：%s" % (
                        pr.returncode, " | ".join(tail[-3:]))
                    return
            if _http_ok(url, timeout=4):
                self._states[eid] = "ready"
                return
            time.sleep(2)
        with self._lock:
            if self._states.get(eid) in ("stopping", "off"):
                return
        self._states[eid] = "ready" if self.is_running(eid) else "error"
        if self._states[eid] == "error":
            self._errors[eid] = "等待就绪超时（%ss），探针 %s 无响应" % (d.get("ready_timeout"), url)

    # ---------------- 停止 ----------------
    # 停止指定引擎：优雅中断 → taskkill /T 杀整棵进程树 → 等端口释放 → 等显存回落。
    def stop(self, eid, timeout=30):
        with self._lock:
            return self._stop_locked(eid, timeout=timeout)

    def _stop_locked(self, eid, timeout=30):
        d = self.defn(eid)
        with self._lock:
            info = dict(self._procs.get(eid) or {})
            self._states[eid] = "stopping"
        pid = info.get("pid") or _pid_on_port(d.get("port"), ttl=0)
        external = not bool(info)
        result = {"ok": True, "pid": pid, "external": external,
                  "name": d.get("name") or eid, "killed": False}

        # 1) 优雅中断：让正在跑的任务先停下（ComfyUI 有 /interrupt）
        if d.get("interrupt_url"):
            try:
                req = urllib.request.Request(d["interrupt_url"], data=b"{}",
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=6).read()
                result["interrupted"] = True
            except Exception:
                pass

        # 2) 杀整棵进程树
        if pid:
            _kill_tree(pid)
            result["killed"] = True
        _cache_clear("netstat")   # 刚杀完，快照必须作废

        # 3) 等端口释放（要实时值，刚 taskkill 完缓存还是旧的）
        t0 = time.time()
        while time.time() - t0 < max(8, timeout * 0.6):
            if _pid_on_port(d.get("port"), ttl=0) is None:
                break
            time.sleep(1)
        result["port_free"] = _pid_on_port(d.get("port"), ttl=0) is None

        # 4) 等显存回落（只对绘图/大模型引擎有意义）
        g = gpu_info(ttl=0)
        if g and g["used"] > VRAM_RELEASED_GB:
            t0 = time.time()
            while time.time() - t0 < 25:
                g2 = gpu_info(ttl=0)
                if not g2 or g2["used"] <= VRAM_RELEASED_GB:
                    break
                time.sleep(2)
        with self._lock:
            self._procs.pop(eid, None)
            self._states[eid] = "off"
        result["gpu"] = gpu_info(ttl=0)
        if not result["port_free"]:
            result["ok"] = False
            result["error"] = "进程未能完全退出，端口 %s 仍在监听" % d.get("port")
        return result

    # 停止全部引擎（退出前清理用）。
    def stop_all(self, timeout=30):
        out = []
        for eid in list(self.running_ids()):
            out.append(self.stop(eid, timeout=timeout))
        return out

    def restart(self, eid, force=False):
        self.stop(eid)
        time.sleep(1.5)
        return self.start(eid, force=force)

    # ---------------- 模型库（外部 GGUF 链接进来） ----------------
    def register_model(self, src_path, link=True):
        """把外部 gguf 目录/文件链接进 X:\\你的llama.cpp\\models（mklink /J 目录联接，零拷贝）。"""
        d = self.defn("llm")
        lib = d.get("models_dir") or r"X:\你的llama.cpp\models"
        if not os.path.isdir(lib):
            return {"ok": False, "error": "模型库目录不存在：%s" % lib}
        src = os.path.abspath(src_path)
        if not os.path.exists(src):
            return {"ok": False, "error": "源路径不存在：%s" % src}
        name = os.path.basename(src.rstrip("\\/"))
        dst = os.path.join(lib, name)
        if os.path.exists(dst):
            return {"ok": False, "error": "模型库里已存在同名项：%s" % dst}
        if not link:
            return {"ok": False, "error": "当前仅支持链接（不复制）"}
        if os.path.isdir(src):
            cmd = ["cmd", "/c", "mklink", "/J", dst, src]
        else:
            cmd = ["cmd", "/c", "mklink", dst, src]
        out, err, rc = _run(cmd, timeout=30)
        if rc != 0:
            return {"ok": False, "error": (err or out or "mklink 失败，可能需要管理员权限")}
        return {"ok": True, "linked": dst, "target": src, "output": (out + err).strip()}


__all__ = ["EngineManager", "default_engines", "gpu_info", "build_llm_args",
           "scan_models", "llm_active_model", "llm_vram_est"]
