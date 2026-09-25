#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comfy_studio.py —— 自带的本地 Web 界面后端（文生图 / 图生图 / MiniMax H3 视频）。

前端是一个单页 HTML（同目录 comfy_studio.html），本文件是它后端：
  * 文生图 / 图生图  → 直连本机 ComfyUI（复用 comfy_control.py 的 UI→API 转换 + /prompt 提交，
    已实测 Qwen-Image 2.1 工作流出图成功）。
  * H3 视频          → 走本机 ComfyUI(8188) 的 MiniMax-H3 原生节点（与文生图共用同一个
    引擎实例）。**16GB 显存实测可跑**：RTX 5060Ti 16GB 峰值显存约 14GB（见
    2026-09-23 验收报告）。模型文件总量虽 ~64GB，但引擎按需加载/卸载，不会整包常驻显存。
    另可选「远程端点」模式：把六段式提示词 + 参考图 + 参数 POST 给另一台显存更大的机器上的
    llama-server，再轮询结果（该模式才需要显存 ≥48GB 的机器）。

零额外依赖：只用 Python 标准库 + 已装好的 comfy-cli（venv 里）做 UI→API 转换。

启动：  python comfy_studio.py
然后浏览器打开 http://127.0.0.1:8777

注意：会清掉环境里残留(可能已失效)的 HTTP(S)_PROXY，避免 localhost:8188 被代理拦截。
"""
import base64
import json
import os
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.parse as urllib_parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- 1) 关键：清代理，放行 localhost（本机 Mihomo 代理曾把 8188 误拦成 502）---
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
os.environ.setdefault("PYTHONUTF8", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 用户目录：打包成 exe 后 = exe 同级；直接跑脚本时 = 脚本目录。
# 内置资源（comfy_studio.html / comfy_control.py）始终在 HERE；用户配置与数据放 HOME_DIR。
HOME_DIR = os.environ.get("COMFYSTUDIO_HOME") or HERE
DATA_DIR = os.path.join(HOME_DIR, "data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# 无控制台运行（pythonw）时 sys.stdout 是 None，任何 print() 都会抛异常 →
# 兜底重定向到 logs/backend.log。正常情况下启动器用的是 python.exe + 隐藏控制台，
# 不会走到这里，但这一层能让"直接双击 pythonw 跑本文件"也不炸。
if sys.stdout is None or sys.stderr is None:
    try:
        _logdir = os.path.join(HOME_DIR, "logs")
        os.makedirs(_logdir, exist_ok=True)
        _f = open(os.path.join(_logdir, "backend.log"), "a", encoding="utf-8", buffering=1)
        if sys.stdout is None:
            sys.stdout = _f
        if sys.stderr is None:
            sys.stderr = _f
    except Exception:
        pass


def _config_path():
    """配置查找顺序：环境变量指定 > 用户目录 > 内置（脚本目录）。"""
    p = os.environ.get("COMFYSTUDIO_CONFIG")
    if p and os.path.isfile(p):
        return p
    cand = os.path.join(HOME_DIR, "comfy_studio_config.json")
    if os.path.isfile(cand):
        return cand
    return os.path.join(HERE, "comfy_studio_config.json")


# 先读配置，把「多个输出目录」传给 comfy_control（必须在 import cc 之前设置）
try:
    with open(_config_path(), "r", encoding="utf-8") as _f:
        _cfg0 = json.load(_f)
    _ods = _cfg0.get("output_dirs") or ([_cfg0["output_dir"]] if _cfg0.get("output_dir") else [])
    if _ods:
        os.environ["COMFY_OUTPUT"] = ";".join(_ods)
except Exception:
    pass

import comfy_control as cc  # 复用已验证的 UI→API 转换、直连提交、上传等

CONFIG_PATH = _config_path()
CONFIG = {}
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
except Exception as e:
    print("[WARN] 读不到配置 %s：%s，用内置默认值" % (CONFIG_PATH, e))

COMFY_URL = CONFIG.get("comfy_url", "http://127.0.0.1:8188")
WORKFLOWS = CONFIG.get("workflows", {})
OUTPUT_DIR = CONFIG.get("output_dir", r"X:\你的ComfyUI输出目录")
# 多输出目录：Comfy Desktop 与无头启动的默认目录不同，都要扫（否则画廊空）
OUTPUT_DIRS = list(getattr(cc, "OUTPUT_DIRS", None) or [OUTPUT_DIR])
OUT_DIR = CONFIG.get("out_dir", r"X:\你的出图目录")
H3 = CONFIG.get("h3", {})
H3_COMFY_URL = (H3.get("comfy_url") or "").rstrip("/")
# H3 引擎输出目录。默认跟 ComfyUI 的 output 走（H3 现在跑在 8188，不再是旧整合包的 _internal\output）
H3_OUT_DIR = (H3.get("output_dir") or OUTPUT_DIR
              or (os.path.join(os.path.dirname(H3["engine_exe"]), "_internal", "output")
                  if H3.get("engine_exe") else ""))
LISTEN_HOST = CONFIG.get("listen_host", "0.0.0.0")
LISTEN_PORT = int(CONFIG.get("listen_port", 8777))

# --- 对标参考整合包新增：默认值 / 模型注册表 / 标签库 / 提示词优化器 / 归档 ---
DEFAULTS = CONFIG.get("defaults", {})
NEGATIVE_DEFAULT = CONFIG.get("negative_default", "")
MODELS = CONFIG.get("models", {"unets": [], "loras": []})
MODE_ARCHS = CONFIG.get("mode_archs", {})
ENGINES = CONFIG.get("engines", {})
ARCH_WF = CONFIG.get("arch_workflows", {})
TAG_LIBRARY_PATH = CONFIG.get("tag_library_path", "")
OPT = CONFIG.get("prompt_optimizer", {})
# 优化后提示词的语言：zh=中文（默认，用户要求固定中文） / en=英文
_LANG = (OPT.get("prompt_language") or "zh").strip().lower()
# H3 视频单独一个开关：官方 guide 与参考样例全是英文，默认仍用英文
_H3_LANG = (OPT.get("h3_prompt_language") or "en").strip().lower()
ARCHIVE_ROOT = CONFIG.get("archive_root", "")
ARCHIVE_BY_MODULE = bool(CONFIG.get("archive_by_module", False))

# --- 引擎生命周期管理（启动/停止/互斥/真实检测）→ comfy_engine.py ---
try:
    import comfy_engine as _ceng
except Exception as _e:  # 缺模块不应影响绘图主流程
    _ceng = None
    print("[WARN] comfy_engine 导入失败：%s" % _e)

CHAT_TARGETS = CONFIG.get("chat_targets", {}) or {}
ENGINE_MANAGER = None
_SERVER = None          # main() 里赋值的 HTTPServer，退出流程要用它 shutdown()

# --- 心跳保活（浏览器版专用）------------------------------------------------- #
# 问题：浏览器窗口关掉时，后端收不到任何通知（不像 pywebview 有 closing 事件）。
# 解法：页面每 5s 打一次心跳；后端超过 N 秒收不到心跳、且没有引擎在跑 → 自杀。
# 这样「关掉窗口 → 引擎和后端一起消失」，也顺带防僵尸进程。
#
# ⚠ 2026-09-23：默认改用「用户日常的 Chrome」开普通标签页后，心跳不能卡太死 ——
#    浏览器会把后台标签页的定时器节流到 ~1 次/分钟，HB_TIMEOUT 太小会在用户
#    切去别的标签页时把后端误杀。所以 HB_TIMEOUT 放宽到 120s，同时增加一条
#    「关闭信标 + 心跳已停」的快通道，保证正常关页面仍然秒级收尾。
APP_CFG = CONFIG.get("app") or {}
AUTO_EXIT = bool(APP_CFG.get("auto_exit_no_heartbeat", True))
HB_TIMEOUT = float(APP_CFG.get("heartbeat_timeout_sec", 45))
HB_GRACE = float(APP_CFG.get("heartbeat_grace_sec", 90))   # 后端刚起时的宽限期
# ⚠ 2026-09-23：默认改用「用户日常的 Chrome」开普通标签页后，收尾逻辑必须重做：
#    * 浏览器会把后台标签页的定时器节流到 ~1 次/分钟 → 心跳不能卡太死（HB_TIMEOUT 放宽）
#    * **一定会有多个标签页**（用户自己开过 + 启动器又开一个）→ 不能再用
#      「某一次关闭信标」当全局死讯，否则关掉副标签页会把主标签页一起判死。
#    所以改成**按页面 pid 记账的注册表**：见下面 _PAGES / pages_alive()。
APP_CFG = CONFIG.get("app") or {}
AUTO_EXIT = bool(APP_CFG.get("auto_exit_no_heartbeat", True))
HB_TIMEOUT = float(APP_CFG.get("heartbeat_timeout_sec", 45))
HB_GRACE = float(APP_CFG.get("heartbeat_grace_sec", 90))   # 后端刚起时的宽限期
PAGE_STALE = float(APP_CFG.get("page_stale_sec", 180))     # 页面多久没消息算它没了（>后台节流 1/min）
PAGE_EMPTY_HOLD = float(APP_CFG.get("page_empty_hold_sec", 15))  # 「零页面」要持续这么久才收尾（防刷新竞态）
_HB = {"t": time.time(), "n": 0, "left": 0.0}
_HB_LOCK = threading.Lock()
_QUITTING = {"v": False}
_PAGES = {}                       # page_id -> 最后一次「还活着」的时间
_PAGES_LOCK = threading.Lock()
_PAGE_EMPTY_AT = {"t": 0.0}       # 第一次观察到「零页面」的时刻


def hb_touch():
    with _HB_LOCK:
        _HB["t"] = time.time()
        _HB["n"] += 1


def hb_age():
    with _HB_LOCK:
        return time.time() - _HB["t"]


def hb_left():
    """页面「关闭」信标（前端 pagehide + sendBeacon 打到 /api/app/pagehide）。

    为什么需要它：关窗那一刻 age 还很小（页面刚打过心跳），光看 age 没法区分
    「窗口关了」和「页面还活着」。这个信号由页面自己在卸载时发出，最直接。
    """
    with _HB_LOCK:
        _HB["left"] = time.time()


def hb_left_age():
    """距上次收到「页面关闭」信标的秒数；从未收到过返回 -1。

    ⚠ 只作诊断用，**不再**参与「该不该退出」的判定 —— 多标签页下
    一次关闭信标代表不了全部页面都走了（见 pages_alive()）。
    """
    with _HB_LOCK:
        return round(time.time() - _HB["left"], 1) if _HB["left"] else -1


# ---------- 页面注册表：多标签页安全的「还有人在看吗」 ----------
def page_open(pid):
    """页面加载时登记自己。返回当前活着的页面数。"""
    if not pid:
        return pages_alive()
    with _PAGES_LOCK:
        _PAGES[pid] = time.time()
        return len(_PAGES)


def page_seen(pid):
    """页面心跳时续期。没登记过的（比如刷新后老 pid）顺手补登记。"""
    if not pid:
        return False
    with _PAGES_LOCK:
        _PAGES[pid] = time.time()
        return True


def page_close(pid):
    """页面卸载（pagehide + sendBeacon）时注销**它自己**。

    只删这一个 pid —— 其它还开着的标签页不受影响，这就是多标签页安全的关键。
    """
    if not pid:
        return pages_alive()
    with _PAGES_LOCK:
        _PAGES.pop(pid, None)
        return len(_PAGES)


def pages_alive():
    """还在打心跳的页面数（顺手清掉超期的）。

    PAGE_STALE 必须明显大于浏览器对后台标签页的节流周期（~60s），
    否则用户切去别的标签页一会儿，这里就误判成「没人看了」。
    """
    now = time.time()
    with _PAGES_LOCK:
        for k in [k for k, v in _PAGES.items() if now - v > PAGE_STALE]:
            _PAGES.pop(k, None)
        return len(_PAGES)


def pages_detail():
    now = time.time()
    with _PAGES_LOCK:
        return [{"pid": k, "age": round(now - v, 1)} for k, v in _PAGES.items()]


def heartbeat_state():
    return {"enabled": AUTO_EXIT, "timeout": HB_TIMEOUT, "age": round(hb_age(), 1),
            "beats": _HB["n"], "left": hb_left_age(),
            "pages": pages_alive(), "pages_detail": pages_detail(),
            "page_stale": PAGE_STALE}


def _qget(path, key):
    """从 `/api/xxx?a=1&b=2` 里取一个查询参数（自己解析，不额外依赖）。"""
    if "?" not in path:
        return ""
    q = path.split("?", 1)[1].split("#", 1)[0]
    for kv in q.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            if k == key:
                return v
    return ""


def _hb_watchdog(started_at):
    """心跳守望：没页面在看 + 没引擎在跑 → 自动退出（省电、防僵尸）。

    判定改成**看页面注册表**（pages_alive()），不再看单一心跳时间戳：
      * 还有任何一个标签页活着（含被后台节流、几十秒才打一次的那种）→ 不退；
      * 全部页面都注销了 / 都超期了 → 停引擎并退出。
    这条刻意放在 busy 判断**之前**：页面都关了就该停引擎（对应 config 的
    close_action=stop_engines），不能因为「引擎还在跑」就赖着不走。
    """
    while True:
        time.sleep(5)
        if not AUTO_EXIT or _QUITTING["v"]:
            continue
        if time.time() - started_at < HB_GRACE:
            continue

        # --- 快通道：一个页面都不剩了 ---
        # 零页面必须**持续** PAGE_EMPTY_HOLD 秒才收尾：
        # 刷新页面时 pagehide（注销）→ load（登记）之间有几秒空档，不能一空就退。
        if pages_alive() == 0:
            if not _PAGE_EMPTY_AT["t"]:
                _PAGE_EMPTY_AT["t"] = time.time()
            elif time.time() - _PAGE_EMPTY_AT["t"] >= PAGE_EMPTY_HOLD:
                print("[HB] 连续 %.0fs 没有任何页面在看（注册表已空）→ 停引擎并退出"
                      % PAGE_EMPTY_HOLD)
                request_quit("no-page")
                return
        else:
            _PAGE_EMPTY_AT["t"] = 0.0

        try:
            busy = bool(ENGINE_MANAGER and ENGINE_MANAGER.running_ids())
        except Exception:
            busy = False
        if busy:
            continue
        if hb_age() > HB_TIMEOUT:
            print("[HB] %s 秒没有心跳且无引擎运行 → 自动退出" % int(HB_TIMEOUT))
            request_quit("heartbeat-timeout")
            return


def request_quit(reason="api"):
    """停引擎 → 关服务 → 进程退出（幂等）。"""
    if _QUITTING["v"]:
        return
    _QUITTING["v"] = True
    print("[QUIT] 收到退出请求（%s）" % reason)

    def _do():
        try:
            ids = ENGINE_MANAGER.running_ids() if ENGINE_MANAGER else []
            if ids:
                print("[QUIT] 停止引擎：%s" % ids)
                ENGINE_MANAGER.stop_all()
                print("[QUIT] 引擎已停止")
        except Exception as e:
            print("[QUIT] 停引擎出错：%s" % e)
        time.sleep(0.6)
        try:
            globals().get("_SERVER") and _SERVER.shutdown()
        except Exception:
            pass
        print("[QUIT] 后端已停止")
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(0)

    threading.Thread(target=_do, daemon=True).start()



def init_engine_manager(cfg=None):
    """创建全局引擎管理器（API 路由与关窗清理共用同一实例）；重复调用返回既有实例。"""
    global ENGINE_MANAGER, CHAT_TARGETS
    if ENGINE_MANAGER is not None:
        return ENGINE_MANAGER
    if _ceng is None:
        return None
    c = cfg or CONFIG
    CHAT_TARGETS = c.get("chat_targets", {}) or {}
    app = (c.get("app") or {})
    ENGINE_MANAGER = _ceng.EngineManager(
        engines_cfg=c.get("engines_launch") or {},
        log_dir=os.path.join(HOME_DIR, "logs"),
        exclusive=app.get("exclusive") or "all",
    )
    return ENGINE_MANAGER


def store_path(key):
    safe = re.sub(r"[^0-9A-Za-z_.\-]", "_", str(key))[:64]
    d = os.path.join(DATA_DIR, "store")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, safe + ".json")


def store_get(key, default=None):
    p = store_path(key)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def store_put(key, value):
    p = store_path(key)
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False, indent=1)
        return True, ""
    except Exception as e:
        return False, str(e)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def http_json(url, payload=None, timeout=120, headers=None):
    import urllib.request
    data = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def http_get(url, timeout=30):
    import urllib.request
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return r.read()


def upload_to_comfy(name, data_bytes, subfolder="", base=None):
    """把图片字节上传到 ComfyUI 的 input 目录，返回 LoadImage 可填的文件名。"""
    boundary = "----comfystudio" + uuid.uuid4().hex
    CRLF = b"\r\n"
    parts = []
    for k, v in (("overwrite", "true"), ("subfolder", subfolder)):
        parts.append(("--%s" % boundary).encode() + CRLF
                     + ('Content-Disposition: form-data; name="%s"' % k).encode() + CRLF + CRLF
                     + str(v).encode() + CRLF)
    parts.append(("--%s" % boundary).encode() + CRLF
                 + ('Content-Disposition: form-data; name="image"; filename="%s"' % name).encode()
                 + CRLF + b"Content-Type: application/octet-stream" + CRLF + CRLF
                 + data_bytes + CRLF)
    parts.append(("--%s--" % boundary).encode() + CRLF)
    body = b"".join(parts)
    import urllib.request
    req = urllib.request.Request(
        (base or COMFY_URL) + "/upload/image", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=600) as r:
        res = json.load(r)
    out = res.get("name", name)
    if res.get("subfolder"):
        out = res["subfolder"] + "/" + out
    return out


def apply_opts(wf, opts):
    """把前端来的参数写进工作流（兼容 subgraph 子图）。返回修改日志。"""
    log = []

    def set_first(name, value):
        ids = cc.find_widget_nodes(wf, name)
        if not ids:
            return False
        nid = ids[0]
        for n in cc.walk_nodes(wf):
            if str(n.get("id")) == str(nid):
                if cc.set_widget(n, name, value):
                    log.append("%s.%s = %s" % (nid, name, json.dumps(value, ensure_ascii=False)))
                    return True
        return False

    if opts.get("prompt") is not None:
        set_first("prompt", opts["prompt"])
    if opts.get("negative") is not None:
        set_first("negative_prompt", opts["negative"])
    if opts.get("seed") is not None:
        set_first("seed", opts["seed"])
    if opts.get("steps") is not None:
        set_first("steps", opts["steps"])
    if opts.get("cfg") is not None:
        set_first("cfg", opts["cfg"])
    if opts.get("batch_size") is not None:
        set_first("batch_size", opts["batch_size"])
    if opts.get("resolution") is not None:
        set_first("resolution", opts["resolution"])
    refs = opts.get("refs") or []
    if refs:
        img_nodes = cc.find_widget_nodes(wf, "image")
        for i, nid in enumerate(img_nodes):
            fn = refs[i % len(refs)]
            for n in cc.walk_nodes(wf):
                if str(n.get("id")) == str(nid):
                    if cc.set_widget(n, "image", fn):
                        log.append("%s.image = %s" % (nid, fn))

    # 底模切换：写进 UnetLoaderGGUF 的 unet_name
    unet = (opts.get("unet_name") or "").strip()
    if unet:
        hit = False
        for nid in cc.find_widget_nodes(wf, "unet_name"):
            for n in cc.walk_nodes(wf):
                if str(n.get("id")) == str(nid) and cc.set_widget(n, "unet_name", unet):
                    log.append("%s.unet_name = %s" % (nid, unet))
                    hit = True
        if not hit:
            log.append("[提示] 工作流内无 unet_name 节点，底模未切换")

    # LoRA：统一交给 inject_lora() 在 API 图上自动串联 LoraLoaderModelOnly
    # （不再依赖工作流里预先存在 LoraLoader 节点，模板工作流同样生效）
    return log


def rel_path(full, base):
    try:
        return os.path.relpath(full, base).replace("\\", "/")
    except Exception:
        return os.path.basename(full)


def url_for(p):
    """把产物绝对路径映射成 /outputs/<目录索引>/<相对路径>（多输出目录下唯一且无 ../）。"""
    for i, od in enumerate(OUTPUT_DIRS):
        try:
            rp = os.path.relpath(p, od)
        except Exception:
            continue
        if not rp.startswith(".."):
            return "/outputs/%d/%s" % (i, rp.replace("\\", "/"))
    return "/outputs/0/" + os.path.basename(p)


# --------------------------------------------------------------------------- #
# 核心：文生图 / 图生图
# --------------------------------------------------------------------------- #
def _ext(p):
    return os.path.splitext(p or "")[1].lower()


def adapt_loaders(api_prompt, unet_name=None):
    """按所选底模格式，动态切换 GGUF / safetensors 加载器。
    社区铁律：.gguf 只能配 UnetLoaderGGUF、.safetensors 只能配 UNETLoader（不能互换）。
    文本编码器同理：.gguf → CLIPLoaderGGUF，其余 → CLIPLoader。
    这样同一套工作流既能吃 Q4_K_M.gguf，也能吃 FP8/FP16 safetensors。
    """
    if not unet_name:
        return api_prompt, []
    log = []
    want_gguf = _ext(unet_name) == ".gguf"
    for nid, node in (api_prompt or {}).items():
        ct = node.get("class_type")
        ins = node.setdefault("inputs", {})
        if ct in ("UnetLoaderGGUF", "UnetLoaderGGUFAdvanced", "UNETLoader"):
            if want_gguf and ct != "UnetLoaderGGUF":
                node["class_type"] = "UnetLoaderGGUF"
                ins.pop("weight_dtype", None)
                log.append("节点%s 底模为 GGUF → 用 UnetLoaderGGUF" % nid)
            elif (not want_gguf) and ct != "UNETLoader":
                node["class_type"] = "UNETLoader"
                ins.setdefault("weight_dtype", "default")
                log.append("节点%s 底模为 safetensors → 用 UNETLoader" % nid)
        elif ct in ("CLIPLoader", "CLIPLoaderGGUF"):
            cname = ins.get("clip_name") or ""
            if _ext(cname) == ".gguf" and ct != "CLIPLoaderGGUF":
                node["class_type"] = "CLIPLoaderGGUF"
                ins.pop("device", None)
                log.append("节点%s TE 为 GGUF → 用 CLIPLoaderGGUF" % nid)
            elif _ext(cname) != ".gguf" and ct != "CLIPLoader":
                node["class_type"] = "CLIPLoader"
                log.append("节点%s TE 为 safetensors → 用 CLIPLoader" % nid)
    return api_prompt, log


def engine_url(key):
    u = (ENGINES.get(key) or "").rstrip("/")
    if u:
        return u
    return H3_COMFY_URL if key == "alt" else COMFY_URL


def arch_of_unet(name):
    for m in (MODELS.get("unets") or []):
        if m.get("name") == name:
            return m.get("arch") or ""
    return ""


def engine_api(base, path, payload=None, timeout=120):
    import urllib.request
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(auth_headers(base))          # URL 内 user:pass@ → Basic（外部既有 ComfyUI）
    req = urllib.request.Request(clean_base(base) + path, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def _comfy_input_exists(fname):
    """用 ComfyUI 的 /view 接口权威判断 input 目录里是否真有这张图。

    图生图工作流的 LoadImage 节点常带着官方模板默认图名（如
    portrait_model_denim.png），那些文件并不在用户的 ComfyUI input 目录里。
    若直接提交，ComfyUI 会回一个晦涩的 'Invalid image file' →
    'prompt_outputs_failed_validation'。这里在提交前先拦下来，给出清晰中文报错。
    """
    import urllib.error
    import urllib.parse
    import urllib.request
    if not fname:
        return False
    url = "%s/view?filename=%s&type=input&subfolder=" % (
        COMFY_URL, urllib.parse.quote(str(fname)))
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def _comfy_error(status):
    """从 ComfyUI history 的 status 里提取执行错误。
    ⚠️ 失败时 completed=False、status_str="error"，若不显式判断，轮询会一直等下去（踩过）。"""
    if not isinstance(status, dict):
        return ""
    if status.get("status_str") == "error":
        for m in (status.get("messages") or []):
            try:
                if m and str(m[0]) == "execution_error":
                    d = m[1] if len(m) > 1 and isinstance(m[1], dict) else {}
                    return "%s @ 节点 %s(%s)：%s" % (d.get("exception_type"), d.get("node_id"),
                                                    d.get("node_type"),
                                                    str(d.get("exception_message"))[:400])
            except Exception:
                pass
        return "引擎报告执行失败（无详细信息）"
    return ""


def build_minimal_t2i(cfg, opts):
    """给没有现成工作流的架构（Krea-2 / Ideogram-4 等）拼一个最小 txt2img 图。"""
    w = int(opts.get("resolution") or cfg.get("width") or 1024)
    h = int(opts.get("height") or cfg.get("height") or w)
    steps = int(opts.get("steps") or cfg.get("steps") or 8)
    cv = opts.get("cfg")
    cv = float(cv) if cv is not None else float(cfg.get("cfg", 1.0))
    seed = opts.get("seed")
    seed = int(seed) if seed is not None else -1
    if seed < 0:
        seed = int(time.time() * 1000) % (2 ** 31)

    unet = cfg.get("unet") or ""
    if _ext(unet) == ".gguf":
        loader = {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": unet}}
    else:
        loader = {"class_type": "UNETLoader",
                  "inputs": {"unet_name": unet, "weight_dtype": "default"}}
    cn = cfg.get("clip_name") or ""
    ctype = cfg.get("clip_type") or "stable_diffusion"
    if _ext(cn) == ".gguf":
        clipn = {"class_type": "CLIPLoaderGGUF", "inputs": {"clip_name": cn, "type": ctype}}
    else:
        clipn = {"class_type": "CLIPLoader", "inputs": {"clip_name": cn, "type": ctype}}

    graph = {
        "1": loader,
        "2": clipn,
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": cfg.get("vae_name") or "ae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["2", 0], "text": opts.get("prompt") or ""}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": opts.get("negative") or NEGATIVE_DEFAULT}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": int(opts.get("batch_size") or 1)}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["6", 0], "seed": seed, "steps": steps, "cfg": cv,
                         "sampler_name": cfg.get("sampler") or "euler",
                         "scheduler": cfg.get("scheduler") or "simple", "denoise": 1.0}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}},
        "9": {"class_type": "SaveImage",
              "inputs": {"images": ["8", 0], "filename_prefix": "ComfyStudio_" + ctype}},
    }
    return graph, {"engine": cfg.get("engine"), "unet": os.path.basename(unet), "clip_type": ctype,
                   "steps": steps, "cfg": cv, "width": w, "height": h, "seed": seed}


# --------------------------------------------------------------------------- #
# 运行日志：统一入口（内存环形缓冲 → 前端左栏子栏增量拉取）+ 按天落盘
# --------------------------------------------------------------------------- #
LOG_DIR = os.path.join(HOME_DIR, "logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    pass

LOG_RING = []                      # [{seq,t,ts,lvl,tag,msg}]，前端按 seq 增量取
LOG_SEQ = [0]
# ⚠️ 必须用 RLock：log_public() 持锁后再调 log_since()，两者都要这把锁。
#    用普通 Lock 会自锁死（实测：进程静默卡死、整个界面无响应）。
LOG_LOCK = threading.RLock()
LOG_RING_MAX = 1500
_LOG_FH = {"day": "", "fh": None}
# 级别：i 信息 / k 关键（任务起止、出图）/ w 警告 / e 错误
LOG_TAGS = ("task", "prog", "ws", "ui", "engine", "img", "h3", "chat", "cfg", "app")


def _log_file_write(day, line):
    """按天切分落盘：logs/ui-YYYYMMDD.log（句柄缓存，跨天自动换）。"""
    try:
        if _LOG_FH["day"] != day or _LOG_FH["fh"] is None:
            if _LOG_FH["fh"] is not None:
                try:
                    _LOG_FH["fh"].close()
                except Exception:
                    pass
            _LOG_FH["fh"] = open(os.path.join(LOG_DIR, "ui-%s.log" % day), "a",
                                 encoding="utf-8", buffering=1)
            _LOG_FH["day"] = day
        _LOG_FH["fh"].write(line)
    except Exception:
        pass


def log_add(tag, msg, lvl="i"):
    """统一日志入口：环形缓冲 + 落盘 + 控制台。返回事件序号。"""
    msg = str(msg)
    if len(msg) > 4000:
        msg = msg[:4000] + "…"
    now = time.time()
    day = time.strftime("%Y%m%d", time.localtime(now))
    hhmmss = time.strftime("%H:%M:%S", time.localtime(now))
    with LOG_LOCK:
        LOG_SEQ[0] += 1
        e = {"seq": LOG_SEQ[0], "t": round(now, 3), "ts": hhmmss, "lvl": lvl,
             "tag": tag, "msg": msg}
        LOG_RING.append(e)
        if len(LOG_RING) > LOG_RING_MAX:
            del LOG_RING[:len(LOG_RING) - LOG_RING_MAX]
        _log_file_write(day, "%s [%s] %-6s %s\n" % (hhmmss, lvl, tag, msg))
    try:
        print("[%s] %s: %s" % (lvl, tag, msg), flush=True)
    except Exception:
        pass
    return e["seq"]


def log_since(since=0, limit=800):
    """取 seq > since 的日志行（前端轮询增量）。"""
    since = int(since or 0)
    with LOG_LOCK:
        return [dict(e) for e in LOG_RING if e["seq"] > since][-int(limit):]


def log_public(since=0, limit=800):
    with LOG_LOCK:
        return {"seq": LOG_SEQ[0], "rows": log_since(since, limit),
                "file": os.path.join(LOG_DIR, "ui-%s.log" % time.strftime("%Y%m%d")),
                "dir": LOG_DIR}


# --------------------------------------------------------------------------- #
# 任务进度：给「进度条 + 预计剩余秒」用（ComfyUI /ws 实时事件 → 全局快照）
# --------------------------------------------------------------------------- #
# 历史耗时（兜底 ETA：模型加载阶段拿不到步数进度时用上一次同类任务的耗时估）
_TASK_HIST = store_get("task_hist", {}) or {}
HIST_LOCK = threading.Lock()

PROG = {
    "seq": 0, "active": False, "label": "", "base": "", "pid": "",
    "t0": 0.0, "t_first": 0.0, "elapsed": 0.0,
    "step": 0, "max": 0, "node": "", "ndone": 0, "ntotal": 0,
    "phase": "空闲", "eta": None, "eta_src": "", "ws": "off", "ws_err": "",
    "last": None,
}
PROG_LOCK = threading.RLock()      # 同 LOG_LOCK：prog_snapshot 持锁时会回调 hist_avg
_PROG_RATE = []          # [(t, step)] 最近约 3s 的采样，用于算实时步速
_PROG_LOG_T = [0.0]      # 上一条进度日志的时间（节流）
_PROG_LOG_STEP = [0]     # 上一条进度日志的步数（防止同一步反复刷）
_ACTIVE_WS = {"t": None, "cid": ""}


def hist_avg(label):
    with HIST_LOCK:
        arr = list(_TASK_HIST.get(label) or [])
    if not arr:
        return None
    arr = arr[-6:]
    return sum(arr) / float(len(arr))


def hist_put(label, secs):
    if not label or not secs or secs <= 0:
        return
    with HIST_LOCK:
        arr = list(_TASK_HIST.get(label) or [])
        arr.append(round(float(secs), 1))
        _TASK_HIST[label] = arr[-12:]
        store_put("task_hist", _TASK_HIST)


def prog_begin(label, base="", phase="排队中"):
    with PROG_LOCK:
        PROG.update({"seq": PROG["seq"] + 1, "active": True, "label": label or "任务",
                     "base": base or "", "pid": "", "t0": time.time(), "t_first": 0.0,
                     "elapsed": 0.0, "step": 0, "max": 0, "node": "",
                     "ndone": 0, "ntotal": 0, "phase": phase,
                     "eta": None, "eta_src": "", "ws": "off", "ws_err": ""})
        del _PROG_RATE[:]
        _PROG_LOG_STEP[0] = 0
        _PROG_LOG_T[0] = 0.0
    ha = hist_avg(label)
    log_add("task", "▶ 开始：%s（引擎 %s%s）" % (
        label or "任务", clean_base(base) or "-",
        "，上次同类耗时 %.0fs" % ha if ha else ""), "k")


def prog_set_pid(pid):
    with PROG_LOCK:
        PROG["pid"] = pid or ""
        if pid and PROG["phase"] == "排队中":
            PROG["phase"] = "已提交，等待执行"


def prog_mark(ok, note=""):
    """把本次任务的结果写好，等 job_end 落账。"""
    with PROG_LOCK:
        PROG["_ok"] = None if ok is None else bool(ok)
        PROG["_note"] = str(note or "")[:300]


def prog_end(note="", ok=None):
    with PROG_LOCK:
        if PROG.get("_ok") is not None and ok is None:
            ok = PROG.get("_ok")
            note = note or PROG.get("_note") or ""
        el = (time.time() - PROG["t0"]) if PROG["t0"] else 0.0
        lb = PROG["label"] or "任务"
        PROG.update({"active": False, "elapsed": round(el, 1),
                     "phase": "完成" if ok else ("失败" if ok is False else "结束"),
                     "last": {"label": lb, "secs": round(el, 1), "ok": bool(ok),
                              "note": note[:300]}})
    if ok and el > 0.5:
        hist_put(lb, el)
    log_add("task", "%s：%s（用时 %.1fs%s）" % (
        lb, "✔ 完成" if ok else ("✘ 失败" if ok is False else "结束"), el,
        "，" + note if note else ""), "k" if ok else "w")


def prog_snapshot():
    """给前端的一帧进度快照（含 ETA 与历史耗时表）。"""
    with PROG_LOCK:
        p = {k: v for k, v in PROG.items() if not k.startswith("_")}
        if p["active"] and p["t0"]:
            p["elapsed"] = round(time.time() - p["t0"], 1)
            if p.get("eta") is None:
                ha = hist_avg(p["label"])
                if ha:
                    p["eta"] = max(0.0, round(ha - p["elapsed"], 1))
                    p["eta_src"] = "hist"
    p["hist"] = {k: round(hist_avg(k) or 0, 1) for k in list(_TASK_HIST)[:16]}
    return p


def _prog_feed(mt, d):
    """把一条 ComfyUI /ws 消息消化进 PROG。"""
    now = time.time()
    with PROG_LOCK:
        if not PROG["active"]:
            return
        if mt == "progress":
            try:
                v = int(d.get("value") or 0)
                mx = int(d.get("max") or 0)
            except Exception:
                v, mx = 0, 0
            if v and not PROG["t_first"]:
                PROG["t_first"] = now
            PROG["step"], PROG["max"] = v, mx
            PROG["node"] = str(d.get("node") or "")
            PROG["phase"] = "采样中"
            _PROG_RATE.append((now, v))
            while len(_PROG_RATE) > 2 and now - _PROG_RATE[0][0] > 3.0:
                _PROG_RATE.pop(0)
            if mx and v:
                if v >= mx:
                    PROG["eta"], PROG["eta_src"] = 0.0, "step"
                elif len(_PROG_RATE) >= 2:
                    dt = _PROG_RATE[-1][0] - _PROG_RATE[0][0]
                    dv = _PROG_RATE[-1][1] - _PROG_RATE[0][1]
                    if dt > 0.05 and dv > 0:
                        PROG["eta"] = round((mx - v) * dt / float(dv), 1)
                        PROG["eta_src"] = "step"
        elif mt == "progress_state":
            nodes = d.get("nodes") or {}
            done = 0
            if isinstance(nodes, dict):
                for nd in nodes.values():
                    if isinstance(nd, dict) and str(nd.get("status") or "") in (
                            "done", "success", "executed", "cached", "complete"):
                        done += 1
            PROG["ndone"], PROG["ntotal"] = done, len(nodes)
        elif mt == "execution_start":
            if PROG["phase"] in ("排队中", "已提交，等待执行"):
                PROG["phase"] = "执行中"
        elif mt == "execution_cached":
            # ⚠ 这条消息在**刚提交时**就会来（只是说"有些节点不用重算"），
            #   早于任何采样步。若把它当主阶段，界面上会出现 7 秒的"命中缓存"，
            #   看着像卡住了。所以只在已经进入采样之后才改阶段。
            if PROG["step"]:
                PROG["phase"] = "命中缓存"
        elif mt == "executing":
            if d.get("node"):
                PROG["node"] = str(d.get("node"))
                if PROG["phase"] in ("排队中", "已提交，等待执行"):
                    PROG["phase"] = "执行中"
            else:
                PROG["phase"] = "收尾（保存产物）"
        elif mt == "execution_success":
            PROG["phase"] = "完成"
        elif mt == "execution_error":
            PROG["phase"] = "执行失败"
        elif mt == "execution_interrupted":
            PROG["phase"] = "已中断"
        v, mx, node = PROG["step"], PROG["max"], PROG["node"]
    if mt == "progress":
        # 节流：只在「步数变了」且（首步 / 末步 / 距上一条 ≥1.2s）时写一行。
        # 光按时间节流不够——同一步可能被反复推送（比如 1/999 卡住），会刷满整个日志栏。
        if v != _PROG_LOG_STEP[0] and (v <= 1 or v >= mx or now - _PROG_LOG_T[0] >= 1.2):
            _PROG_LOG_STEP[0] = v
            _PROG_LOG_T[0] = now
            log_add("prog", "采样 %d/%d 步（节点 %s）" % (v, mx, node or "?"))
    elif mt == "execution_cached":
        log_add("prog", "部分节点命中缓存，跳过重算")
    elif mt == "execution_error":
        log_add("prog", "引擎报错：%s" % json.dumps(d, ensure_ascii=False)[:300], "e")
    elif mt == "execution_interrupted":
        log_add("prog", "引擎报告已中断", "w")


# --------------------------------------------------------------------------- #
# 极简 WebSocket 客户端（纯标准库）：连 ComfyUI /ws 收真实采样进度
# 支持 https/wss 与 URL 内 user:pass@ 的 basic auth（外部既有 ComfyUI 常见）
# --------------------------------------------------------------------------- #
def _ws_target(base):
    u = urllib_parse.urlsplit((base or "").strip())
    host = u.hostname
    if not host:
        raise ValueError("引擎地址无效：%s" % (base,))
    tls = u.scheme in ("https", "wss")
    port = u.port or (443 if tls else 80)
    hdr = ""
    if u.username:
        tok = base64.b64encode(("%s:%s" % (u.username, u.password or "")).encode("utf-8")).decode("ascii")
        hdr = "Authorization: Basic %s\r\n" % tok
    return host, port, tls, hdr


def auth_headers(base):
    """URL 里写了 user:pass@ 时补 Authorization 头（urllib 不会自动带 userinfo）。"""
    try:
        u = urllib_parse.urlsplit(base or "")
        if u.username:
            tok = base64.b64encode(("%s:%s" % (u.username, u.password or "")).encode("utf-8")).decode("ascii")
            return {"Authorization": "Basic " + tok}
    except Exception:
        pass
    return {}


def clean_base(base):
    """去掉 URL 里的 user:pass@（http.client 不接受带 userinfo 的 host）。"""
    b = (base or "").strip()
    try:
        u = urllib_parse.urlsplit(b)
        if u.username:
            netloc = (u.hostname or "") + ((":%d" % u.port) if u.port else "")
            return urllib_parse.urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment))
    except Exception:
        pass
    return b


# --------------------------------------------------------------------------- #
# 运行时切换 ComfyUI 地址（本机 127.0.0.1:8188 ↔ 外部既有 ComfyUI / 反代）
# --------------------------------------------------------------------------- #
def mask_url(u):
    """把 URL 里的密码换成 ***：设置页要回显地址，但明文密码不该躺在截屏和日志里。"""
    try:
        p = urllib_parse.urlsplit(u or "")
        if p.password:
            net = "%s:***@%s" % (p.username or "", p.hostname or "")
            if p.port:
                net += ":%d" % p.port
            return urllib_parse.urlunsplit((p.scheme or "http", net, p.path, p.query, p.fragment))
    except Exception:
        pass
    return u or ""


def unmask_url(new, old):
    """输入框里还是 *** 说明用户没改密码 → 把旧密码接回去，避免"改个端口把密码抹了"。"""
    if "***" not in (new or ""):
        return new
    try:
        pn = urllib_parse.urlsplit(new)
        po = urllib_parse.urlsplit(old or "")
        if po.password and pn.username:
            net = "%s:%s@%s" % (pn.username, po.password, pn.hostname or "")
            if pn.port:
                net += ":%d" % pn.port
            return urllib_parse.urlunsplit((pn.scheme or "http", net, pn.path, pn.query, pn.fragment))
    except Exception:
        pass
    return new


def normalize_url(u):
    """补 scheme、去尾斜杠；返回 (url, err)。"""
    u = (u or "").strip().rstrip("/")
    if not u:
        return "", "地址不能为空"
    if not re.match(r"^https?://", u):
        u = "http://" + u
    try:
        p = urllib_parse.urlsplit(u)
    except Exception as e:
        return "", "地址解析失败：%s" % e
    if not p.hostname:
        return "", "解析不出主机名：%s" % u
    return u, ""


def set_comfy_url(u):
    """切换运行期 ComfyUI 地址。

    ⚠️ 必须**两处一起改**：本模块的 COMFY_URL（状态检测 / 直连 API 用）和
       comfy_control.BASE_URL（comfy_control.direct_submit 提交任务时用它）。
       只改一处会出现「状态显示连上了、真提交还打老地址」的错位，非常难查。
    """
    global COMFY_URL
    u, err = normalize_url(u)
    if err:
        return False, err
    CONFIG["comfy_url"] = u
    COMFY_URL = u
    cb = clean_base(u)
    try:
        cc.BASE_URL = cb
        p = urllib_parse.urlsplit(cb)
        if p.hostname:
            cc.HOST = p.hostname
        if p.port:
            cc.PORT = str(p.port)
    except Exception as e:
        return False, "同步给 comfy_control 失败：%s" % e
    ok, err2 = save_config()
    log_add("cfg", "ComfyUI 地址已切换为 %s%s" % (mask_url(u), "" if ok else "（写盘失败：%s）" % err2),
            "k" if ok else "w")
    return ok, err2


def set_h3_url(u, inherit=True):
    """H3 引擎地址；留空＝跟随 comfy_url（默认就是同一个 8188）。"""
    global H3_COMFY_URL
    u = (u or "").strip()
    if not u:
        H3.pop("comfy_url", None)
        H3_COMFY_URL = "" if inherit else ""
        ok, err = save_config()
        log_add("cfg", "H3 引擎地址已清空 → 跟随 ComfyUI 地址", "k" if ok else "w")
        return ok, err
    u, err = normalize_url(u)
    if err:
        return False, err
    H3["comfy_url"] = u
    H3_COMFY_URL = u
    ok, err2 = save_config()
    log_add("cfg", "H3 引擎地址已切换为 %s" % mask_url(u), "k" if ok else "w")
    return ok, err2


class WSConn:
    """够用即可：文本帧 + ping/pong + 跳二进制（预览图）。"""

    def __init__(self, base, path, timeout=20):
        host, port, tls, ahdr = _ws_target(base)
        s = socket.create_connection((host, port), timeout=timeout)
        if tls:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        self.s = s
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n%s\r\n") % (path, host, port, key, ahdr)
        s.sendall(req.encode("utf-8"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise RuntimeError("握手期间连接被关闭")
            buf += c
        head, _, rest = buf.partition(b"\r\n\r\n")
        line = head.split(b"\r\n")[0]
        if b"101" not in line:
            raise RuntimeError("握手失败：%s" % line.decode("utf-8", "replace"))
        self.buf = rest
        s.settimeout(0.5)

    def _need(self, n):
        while len(self.buf) < n:
            c = self.s.recv(65536)
            if not c:
                raise EOFError
            self.buf += c

    def recv(self):
        """返回 (opcode, data)；超时返回 (None, None)。"""
        while True:
            try:
                self._need(2)
            except (socket.timeout, TimeoutError):
                return None, None
            except EOFError:
                return 0x8, b""
            b0, b1 = self.buf[0], self.buf[1]
            op, masked, ln = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
            off = 2
            if ln == 126:
                self._need(4)
                ln = struct.unpack(">H", self.buf[2:4])[0]
                off = 4
            elif ln == 127:
                self._need(10)
                ln = struct.unpack(">Q", self.buf[2:10])[0]
                off = 10
            self._need(off + (4 if masked else 0) + ln)
            if masked:
                mk = self.buf[off:off + 4]
                off += 4
                data = bytes(self.buf[off + i] ^ mk[i % 4] for i in range(ln))
            else:
                data = bytes(self.buf[off:off + ln])
            self.buf = self.buf[off + ln:]
            if op == 0x9:                                   # ping → pong
                try:
                    self._send(0x8A, data)
                except Exception:
                    return 0x8, b""
                continue
            if op == 0x8:
                return 0x8, data
            if op in (0x1, 0x2):
                return op, data
            continue                                        # 续帧忽略

    def _send(self, op, data):
        n = len(data)
        hdr = bytes([0x80 | op])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mk = os.urandom(4)
        self.s.sendall(hdr + mk + bytes(data[i] ^ mk[i % 4] for i in range(n)))

    def close(self):
        try:
            self._send(0x8, b"")
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass


class ProgWS(threading.Thread):
    """跟着任务跑：用与 /prompt **相同**的 client_id 连 /ws 收进度。

    ⚠️ 必须同 client_id：ComfyUI 的 progress/executing 是定向发给提交者的 sid，
    换个 id 连可能一条都收不到（会白做一条"进度条永远不动"）。
    """

    def __init__(self, base, client_id):
        super().__init__(daemon=True)
        self.name = "progws"
        self.base = base
        self.cid = client_id or ""
        self.stop_ev = threading.Event()

    def stop(self):
        self.stop_ev.set()

    def run(self):
        ws = None
        try:
            ws = WSConn(self.base, "/ws?clientId=" + urllib_parse.quote(self.cid), timeout=20)
            with PROG_LOCK:
                PROG["ws"], PROG["ws_err"] = "ok", ""
            log_add("ws", "进度通道已连上 %s/ws" % clean_base(self.base))
        except Exception as e:
            with PROG_LOCK:
                PROG["ws"], PROG["ws_err"] = "err", str(e)[:200]
            log_add("ws", "进度通道连不上（进度条退回「计时中」样式）：%s" % e, "w")
            return
        try:
            while not self.stop_ev.is_set():
                op, data = ws.recv()
                if op is None:
                    continue
                if op == 0x8:
                    log_add("ws", "进度通道被对端关闭", "w")
                    break
                if op != 0x1:
                    continue
                try:
                    m = json.loads(data.decode("utf-8", "replace"))
                except Exception:
                    continue
                mt = m.get("type") or ""
                d = m.get("data") or {}
                mgpid = str(d.get("prompt_id") or "")
                with PROG_LOCK:
                    mypid = str(PROG["pid"] or "")
                if mgpid and mypid and mgpid != mypid:
                    continue                                # 别人的任务，不理
                _prog_feed(mt, d)
        except Exception as e:
            log_add("ws", "进度通道断开：%r" % (e,), "w")
        finally:
            try:
                ws.close()
            except Exception:
                pass


def prog_ws_start(base):
    """给当前任务开进度通道，返回本次的 client_id（要原样带给 /prompt）。"""
    prog_ws_stop()
    cid = uuid.uuid4().hex
    _ACTIVE_WS["cid"] = cid
    t = ProgWS(base, cid)
    _ACTIVE_WS["t"] = t
    t.start()
    return cid


def prog_ws_stop():
    t = _ACTIVE_WS.get("t")
    _ACTIVE_WS["t"] = None
    if t is not None:
        try:
            t.stop()
        except Exception:
            pass
    return _ACTIVE_WS.get("cid") or ""


def prog_ws_cid():
    return _ACTIVE_WS.get("cid") or "comfystudio"


# --------------------------------------------------------------------------- #
# 暂停生成（向引擎发 /interrupt）+ 打开产物文件夹
# --------------------------------------------------------------------------- #
_CANCEL = {"flag": False, "base": "", "pid": "", "label": ""}
_CANCEL_LOCK = threading.Lock()


def job_begin(base, label=""):
    """开始一个新任务：清掉上次的取消标记，记住本次任务跑在哪个引擎上，
    同时开进度快照与 /ws 进度通道（耗时统计与进度条的数据源）。"""
    with _CANCEL_LOCK:
        _CANCEL.update({"flag": False, "base": base or "", "pid": "", "label": label})
    prog_begin(label, base)
    prog_ws_start(base)


def job_set_pid(pid):
    with _CANCEL_LOCK:
        _CANCEL["pid"] = pid or ""
    prog_set_pid(pid)


def job_cancelled():
    with _CANCEL_LOCK:
        return bool(_CANCEL["flag"])


def job_end(note="", ok=None):
    """收尾：关进度通道、结掉进度快照、清取消标记。"""
    with _CANCEL_LOCK:
        base = _CANCEL["base"]
        _CANCEL.update({"flag": False, "pid": "", "base": "", "label": ""})
    prog_ws_stop()
    try:
        prog_end(note, ok)
    except Exception:
        pass
    return base


def request_cancel():
    """打断当前引擎正在跑的任务，并把还在排队的那条也删掉。"""
    with _CANCEL_LOCK:
        _CANCEL["flag"] = True
        base, pid, label = _CANCEL["base"], _CANCEL["pid"], _CANCEL["label"]
    if not base:
        return {"ok": True, "note": "已标记暂停（当前没有提交到引擎的任务）", "canceled": True}
    steps = []
    s, _b = engine_api(base, "/interrupt", {}, timeout=15)
    steps.append("%s /interrupt → HTTP %s" % (base, s))
    if pid:
        s2, _b2 = engine_api(base, "/queue", {"delete": [pid]}, timeout=15)
        steps.append("队列删除 %s… → HTTP %s" % (str(pid)[:8], s2))
    return {"ok": True, "canceled": True, "engine": base, "label": label, "steps": steps}


def reveal_path(target="", select=False):
    """在资源管理器里打开目录；select=True 且是文件时打开并选中它。"""
    target = (target or "").strip().strip('"')
    if not target:
        return {"ok": False, "error": "路径为空"}
    target = os.path.abspath(target)
    if os.path.isfile(target):
        subprocess.Popen('explorer /select,"%s"' % target)
        return {"ok": True, "mode": "select", "path": target}
    if os.path.isdir(target):
        os.startfile(target)          # noqa: S606  Windows 专用
        return {"ok": True, "mode": "dir", "path": target}
    cur = target                       # 路径不存在 → 退到最近存在的上层目录
    while cur and not os.path.isdir(cur):
        parent = os.path.dirname(cur.rstrip("\\/"))
        if not parent or parent == cur:
            cur = ""
            break
        cur = parent
    if cur:
        os.startfile(cur)
        return {"ok": True, "mode": "parent", "path": cur, "note": "原路径不存在，已打开上层目录"}
    return {"ok": False, "error": "路径不存在：%s" % target}


def submit_poll(base, api_prompt, timeout=3600, out_dirs=None):
    """提交到指定引擎并轮询 /history，返回产物绝对路径。
    注意：标准 SaveImage 的输出**不含 fullpath**，只有 filename/subfolder，需要自己拼。
    """
    cid = prog_ws_cid()                    # 与 /ws 用同一个 client_id，进度才收得到
    s, b = engine_api(base, "/prompt", {"prompt": api_prompt, "client_id": cid}, timeout=120)
    if s != 200:
        log_add("img", "提交失败 HTTP %s → %s" % (s, b[:300]), "e")
        return False, {"error": "提交失败 HTTP %s" % s, "detail": b[:1500]}
    pid = json.loads(b).get("prompt_id")
    job_set_pid(pid)
    log_add("img", "已提交到引擎，prompt_id=%s" % str(pid)[:8])
    start = time.time()
    while time.time() - start < timeout:
        if job_cancelled():
            log_add("img", "用户暂停 → 已中止等待", "w")
            return False, {"cancelled": True, "error": "已暂停生成", "prompt_id": pid}
        time.sleep(4)
        s2, b2 = engine_api(base, "/history/" + pid, timeout=60)
        if s2 != 200:
            continue
        try:
            h = json.loads(b2)
        except Exception:
            continue
        if pid not in h:
            continue
        st = h[pid].get("status") or {}
        errmsg = _comfy_error(st)
        if errmsg:
            log_add("img", "引擎执行失败：%s" % errmsg[:400], "e")
            return False, {"error": "引擎执行失败", "detail": errmsg, "prompt_id": pid}
        if not st.get("completed"):
            continue
        files = []
        for _, out in (h[pid].get("outputs") or {}).items():
            for grp in ("images", "gifs", "videos"):
                for f in (out.get(grp) or []):
                    fp = f.get("fullpath") or ""
                    if not fp or not os.path.isfile(fp):
                        nm = f.get("filename") or ""
                        sub = f.get("subfolder") or ""
                        for od in (out_dirs or OUTPUT_DIRS):
                            cand = os.path.join(od, sub, nm) if sub else os.path.join(od, nm)
                            if nm and os.path.isfile(cand):
                                fp = cand
                                break
                    if fp and os.path.isfile(fp):
                        files.append(fp)
        el = round(time.time() - start, 1)
        log_add("img", "引擎执行完成，用时 %.1fs，产物 %d 个" % (el, len(files)), "k")
        return True, {"prompt_id": pid, "files": files, "status": st.get("status_str"),
                      "elapsed": el}
    log_add("img", "生成超时（%ds 未完成）" % timeout, "e")
    return False, {"error": "生成超时", "prompt_id": pid}


def run_image(mode, opts):
    # 多架构：Krea-2 / Ideogram-4 等用「最小图」提交到对应引擎（它们没有现成工作流文件）
    arch = arch_of_unet(opts.get("unet_name"))
    aw = ARCH_WF.get(arch) or {}
    if aw.get("graph") == "minimal":
        base = engine_url(aw.get("engine") or "alt")
        graph, info = build_minimal_t2i(aw, opts)
        minlog = ["架构 %s → 最小图 → %s" % (arch, base)]
        inject_lora(graph, opts.get("loras"), minlog, unet_name=opts.get("unet_name"))
        before = cc.snapshot_outputs()
        t0 = time.time()
        lb = "%s · %s" % (mode, arch or "未分类")
        job_begin(base, lb)
        ok, res = submit_poll(base, graph, timeout=3600, out_dirs=OUTPUT_DIRS)
        job_end(res.get("error") or "", ok)
        if not ok:
            if res.get("cancelled"):
                return {"ok": False, "cancelled": True, "error": "已暂停生成", "info": info,
                        "engine": base, "log": minlog}
            return {"ok": False, "error": res.get("error"), "detail": res.get("detail"),
                    "info": info, "engine": base, "log": minlog}
        files = res.get("files") or []
        if not files:                      # 兜底：扫输出目录抓新文件
            files = cc.collect_new_outputs(before, t0)
        arr = archive_outputs(mode, os.path.basename(aw.get("unet") or arch), files)
        log_add("img", "出图 %d 张（%s）" % (len(files), lb), "k" if files else "w")
        return {"ok": True, "images": [{"url": url_for(p), "path": p} for p in files],
                "elapsed": res.get("elapsed"), "status": res.get("status"), "info": info,
                "log": minlog, "archived": arr,
                "note": "若 images 为空，ComfyUI 按输入哈希缓存——换 seed 或改任一参数即可强制重跑。"}

    wf_path = WORKFLOWS.get(mode)
    if not wf_path or not os.path.isfile(wf_path):
        return {"ok": False, "error": "找不到工作流：%s（请检查 comfy_studio_config.json 的 workflows.%s）" % (wf_path, mode)}
    with open(wf_path, "r", encoding="utf-8") as f:
        wf = json.load(f)
    # ⚠ 坑：前端「随机种子」默认给 -1，而 ComfyUI 的 KSampler seed 最小值是 0，
    #   原样透传会直接报 "Value -1 smaller than min of 0: seed" 导致生成为空。
    #   所以在写入工作流之前就换成真实的随机正整数。
    try:
        seed = int(opts.get("seed"))
    except (TypeError, ValueError):
        seed = -1
    if seed < 0:
        seed = random.randint(0, 2 ** 32 - 1)
        opts["seed"] = seed
    patch_log = apply_opts(wf, opts)
    patch_log.insert(0, "随机种子 → %d" % seed)
    try:
        api_prompt = cc.convert_ui_to_api(wf)
    except Exception as e:
        return {"ok": False, "error": "UI→API 转换失败：%r" % (e,)}
    # 按所选底模格式切换加载器（GGUF ↔ safetensors）
    api_prompt, loader_log = adapt_loaders(api_prompt, opts.get("unet_name"))
    patch_log += loader_log
    # LoRA：自动串 LoraLoaderModelOnly（先做兼容性自检，不兼容的直接跳过并说明原因）
    inject_lora(api_prompt, opts.get("loras"), patch_log, unet_name=opts.get("unet_name"))

    # ---- 图生图必备：参考图存在性校验 ----
    # 工作流的 LoadImage 节点若还带着模板默认图名（文件根本不在 input 目录），
    # 直接提交会被 ComfyUI 拒成 'Invalid image file' → prompt_outputs_failed_validation。
    # 这里用 /view 接口权威确认，缺图就拦下并给清晰中文提示，不让用户看底层报错。
    if mode == "img2img":
        _bad = []
        for _nid, _node in api_prompt.items():
            if _node.get("class_type") == "LoadImage":
                _fn = (_node.get("inputs") or {}).get("image")
                if not _fn or not _comfy_input_exists(_fn):
                    _bad.append(str(_fn or "<空>"))
        if _bad:
            return {"ok": False,
                    "error": "图生图缺少有效的参考图：%s。请先在左侧「图槽」里点「添加图片」上传（"
                             "不要直接套用工作流自带的示例图名，那些文件并不在你的 ComfyUI input 目录里）。" % "、".join(_bad),
                    "log": patch_log}

    before = cc.snapshot_outputs()
    start = time.time()
    base_std = getattr(cc, "BASE_URL", COMFY_URL)
    lb = "%s · %s" % (mode, os.path.basename(opts.get("unet_name") or "") or "默认")
    job_begin(base_std, lb)
    # 把本次的 client_id 传下去 → 提交与 /ws 同 id，进度事件才收得到
    ok, info = cc.direct_submit(api_prompt, 3600, cancel_check=job_cancelled,
                                on_pid=job_set_pid, client_id=prog_ws_cid())
    _note = ""
    if not ok:
        _note = "已暂停" if (isinstance(info, dict) and info.get("cancelled")) else \
            str((info or {}).get("error_text") or (info or {}).get("error") or "提交/执行失败")
        log_add("img", "标准工作流失败：%s" % _note[:400], "e")
    job_end(_note, ok)
    if not ok:
        if isinstance(info, dict) and info.get("cancelled"):
            return {"ok": False, "cancelled": True, "error": "已暂停生成", "log": patch_log}
        return {"ok": False, "error": "提交/执行失败", "detail": info, "log": patch_log}

    outs = cc.collect_new_outputs(before, start)
    log_add("img", "出图 %d 张（%s）" % (len(outs), lb), "k" if outs else "w")
    # 画廊 URL 一律指向 OUTPUT_DIR 下的原图（保证 /outputs/ 路由能服务，避免 ../../../ 丑路径）
    images = [{"url": url_for(p), "path": p} for p in outs]
    # 顺便复制到 OUT_DIR 方便用户直接取（不影响 URL）
    if OUT_DIR:
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            import shutil
            for p in outs:
                try:
                    shutil.copy2(p, os.path.join(OUT_DIR, os.path.basename(p)))
                except Exception:
                    pass
        except Exception:
            pass
    # 归档：<archive_root>/<模块>/<底模>/<日期>/
    archived = archive_outputs(mode, opts.get("unet_name") or opts.get("model_name"), outs)
    return {
        "ok": True,
        "images": images,
        "elapsed": round(time.time() - start, 1),
        "seed": seed,
        "log": patch_log,
        "archived": archived,
        "note": "若 images 为空，ComfyUI 按输入哈希缓存——换 seed 或改任一参数即可强制重跑。"
    }


# --------------------------------------------------------------------------- #
# 核心：H3 视频（可配置端点，默认本地 llama-server）
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# H3 视频：组装 ComfyUI 工作流 → 提交到 ComfyUI（与文生图共用 8188）
# 实测通过的图：UNETLoader → MiniMaxH3SigmaShift → KSampler
#              MiniMaxH3ImageToVideo/ReferenceToVideo 提供 CONDITIONING+LATENT
#              VAEDecode + VAEDecodeAudio → CreateVideo + SaveVideo（ComfyUI 原生）
# 老引擎（只有 VHS_VideoCombine、没有原生 CreateVideo/SaveVideo）会自动回退 VHS，
# 由 h3_combine_kind() 探测决定。2026-09-23 起不再依赖旧整合包自带的 API 服务 @8288。
# --------------------------------------------------------------------------- #
H3_UNET = H3.get("model_path", "")
H3_TE = "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
H3_VAE_VIDEO = "minimax_h3_video_vae_int8_convrot.safetensors"
H3_VAE_AUDIO = "minimax_h3_audio_vae_fp32.safetensors"


def h3_combine_kind(base):
    """探测目标引擎用哪种「视频合成」节点，结果按 base 缓存。

    优先 **ComfyUI 原生 CreateVideo + SaveVideo**（8188 自带，走 PyAV，零额外依赖）：
    这样别人装一个干净的 ComfyUI 就能跑 H3，不必再装 VideoHelperSuite（它依赖 cv2 /
    imageio-ffmpeg，实测在纯净 venv 里 import 就炸）。只有老引擎没有原生节点时才
    回退到 VHS_VideoCombine。
    """
    if base in _H3_COMBINE_CACHE:
        return _H3_COMBINE_CACHE[base]
    kind = "native"
    try:
        import urllib.request
        with urllib.request.urlopen(base + "/object_info", timeout=180) as r:
            info = json.loads(r.read().decode("utf-8", "replace"))
        if not ("CreateVideo" in info and "SaveVideo" in info):
            kind = "vhs" if "VHS_VideoCombine" in info else "native"
    except Exception:
        kind = "native"
    _H3_COMBINE_CACHE[base] = kind
    return kind


_H3_COMBINE_CACHE = {}


def h3_build_workflow(opts):
    dur = float(opts.get("duration") or 1)
    fps = int(opts.get("fps") or 24)
    length = max(5, int(round(dur * fps)))
    w = int(opts.get("stage1_w") or opts.get("width") or 768)
    h = int(opts.get("stage1_h") or opts.get("height") or 448)
    steps = int(opts.get("steps") or 10)
    seed = opts.get("seed")
    seed = int(seed) if seed is not None else -1
    if seed < 0:
        seed = int(time.time() * 1000) % (2 ** 31)
    prompt = opts.get("prompt") or ""
    refs = [r for r in (opts.get("refs") or []) if r]

    wf = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": H3_UNET, "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": H3_TE, "type": "minimax"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE_VIDEO}},
        "4": {"class_type": "VAELoader", "inputs": {"vae_name": H3_VAE_AUDIO}},
    }
    if refs:
        img_ids = []
        for i, name in enumerate(refs[:9]):
            nid = str(20 + i)
            wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
            img_ids.append([nid, 0])
        wf["5"] = {"class_type": "MiniMaxH3ReferenceToVideo",
                   "inputs": {"clip": ["2", 0], "prompt": prompt, "width": w, "height": h,
                              "length": length, "ref_image_size": "match",
                              "vae": ["3", 0], "audio_vae": ["4", 0], "ref_images": img_ids}}
        mode_note = "Ref2VA（%d 张参考图）" % len(img_ids)
    else:
        wf["5"] = {"class_type": "MiniMaxH3ImageToVideo",
                   "inputs": {"clip": ["2", 0], "vae": ["3", 0], "prompt": prompt,
                              "width": w, "height": h, "length": length}}
        mode_note = "T2VA（纯文生视频+音频）"

    wf["6"] = {"class_type": "MiniMaxH3SigmaShift",
               "inputs": {"model": ["1", 0], "shift_video": 12.0, "shift_audio": 3.0}}
    model_src, nlora = ["6", 0], 0
    for i, spec in enumerate([l for l in (opts.get("loras") or []) if l]):
        nm = spec.get("name") if isinstance(spec, dict) else spec
        nid = str(30 + i)
        wf[nid] = {"class_type": "LoraLoader",
                   "inputs": {"model": model_src, "clip": ["2", 0], "lora_name": nm,
                              "strength_model": float(spec.get("weight", 1.0)) if isinstance(spec, dict) else 1.0,
                              "strength_clip": 1.0}}
        model_src, nlora = [nid, 0], nlora + 1
    wf["7"] = {"class_type": "KSampler",
               "inputs": {"model": model_src, "positive": ["5", 0], "negative": ["5", 0],
                          "latent_image": ["5", 1], "seed": seed, "steps": steps, "cfg": 1.0,
                          "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}}
    wf["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["3", 0]}}
    wf["9"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["7", 0], "vae": ["4", 0]}}
    # 视频合成：原生 CreateVideo+SaveVideo（默认） / VHS_VideoCombine（回退）
    combine = h3_combine_kind(H3_COMFY_URL)
    if combine == "native":
        wf["10"] = {"class_type": "CreateVideo",
                    "inputs": {"images": ["8", 0], "audio": ["9", 0], "fps": float(fps),
                               "codec": "h264"}}
        wf["11"] = {"class_type": "SaveVideo",
                    "inputs": {"video": ["10", 0], "filename_prefix": "ComfyStudio_H3",
                               "format": "auto"}}
    else:
        wf["10"] = {"class_type": "VHS_VideoCombine",
                    "inputs": {"images": ["8", 0], "audio": ["9", 0], "frame_rate": fps,
                               "loop_count": 0, "filename_prefix": "ComfyStudio_H3",
                               "format": "video/h264-mp4", "pingpong": False, "save_output": True}}
    return wf, {"mode_note": mode_note, "width": w, "height": h, "length": length,
                "steps": steps, "seed": seed, "fps": fps, "loras": nlora,
                "combine": combine}


def h3_api(path, payload=None, timeout=120):
    import urllib.request
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(auth_headers(H3_COMFY_URL))
    req = urllib.request.Request(clean_base(H3_COMFY_URL) + path, data=data, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, str(e)


def h3_resolve_output(f, out_dirs):
    """把 ComfyUI history 里的一条产物记录解析成磁盘绝对路径。

    两种形态：
      * VHS_VideoCombine → 直接给 `fullpath`（老逻辑只认这个）；
      * 原生 SaveVideo / SaveImage → 只给 {filename, subfolder, type}，得自己去
        output 目录里拼。所以这里两种都认。
    """
    fp = (f.get("fullpath") or "").strip()
    if fp and os.path.isfile(fp):
        return fp
    fn = (f.get("filename") or "").strip()
    if not fn:
        return ""
    sub = (f.get("subfolder") or "").strip()
    for od in (out_dirs or []):
        cand = os.path.join(od, sub, fn) if sub else os.path.join(od, fn)
        if os.path.isfile(cand):
            return cand
    return ""


VID_EXT = (".mp4", ".webm", ".mkv", ".mov", ".gif", ".png", ".jpg", ".jpeg", ".webp")


def h3_scan_new_videos(out_dirs, before, since):
    """兜底：history 里认不出产物时，直接扫输出目录抓新文件。"""
    news = []
    for od in (out_dirs or []):
        if not os.path.isdir(od):
            continue
        for root, _, files in os.walk(od):
            for f in files:
                if not f.lower().endswith(VID_EXT):
                    continue
                p = os.path.join(root, f)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if p not in before or before.get(p) != m or m >= since:
                    news.append(p)
    return sorted(news, key=lambda p: os.path.getmtime(p))


def h3_run(opts):
    """提交 → 轮询 → 收集产物 → 归档。"""
    wf, info = h3_build_workflow(opts)
    s, b = h3_api("/prompt", {"prompt": wf, "client_id": prog_ws_cid()}, timeout=120)
    if s != 200:
        log_add("h3", "提交失败 HTTP %s → %s" % (s, b[:300]), "e")
        return {"ok": False, "error": "提交失败 HTTP %s" % s, "detail": b[:2000], "info": info}
    pid = json.loads(b).get("prompt_id")
    job_set_pid(pid)
    log_add("h3", "已提交视频任务，prompt_id=%s" % str(pid)[:8])
    start = time.time()
    before = cc.snapshot_outputs()
    deadline = start + int(opts.get("timeout") or 3600)
    while time.time() < deadline:
        if job_cancelled():
            return {"ok": False, "cancelled": True, "error": "已暂停生成", "prompt_id": pid, "info": info}
        time.sleep(5)
        s2, b2 = h3_api("/history/" + pid, timeout=60)
        if s2 != 200:
            continue
        try:
            h = json.loads(b2)
        except Exception:
            continue
        if pid not in h:
            continue
        item = h[pid]
        st = item.get("status") or {}
        errmsg = _comfy_error(st)
        if errmsg:
            log_add("h3", "引擎执行失败：%s" % errmsg[:400], "e")
            return {"ok": False, "error": "H3 引擎执行失败", "detail": errmsg, "prompt_id": pid}
        if not st.get("completed"):
            continue
        files = []
        keys = []
        for _, out in (item.get("outputs") or {}).items():
            keys += list(out.keys())
            for grp in ("gifs", "images", "videos", "video"):
                for f in (out.get(grp) or []):
                    if not isinstance(f, dict):
                        continue
                    fp = h3_resolve_output(f, OUTPUT_DIRS)
                    if fp and fp not in files:
                        files.append(fp)
        if not files:
            # 原生 SaveVideo 的 history 结构可能不带 fullpath → 兜底扫目录
            files = h3_scan_new_videos(OUTPUT_DIRS, before, start)
        archived = archive_outputs("h3", (opts.get("unet_name") or "MiniMax-H3"), files)
        videos = [{"url": url_for(p), "path": p} for p in files
                  if p.lower().endswith((".mp4", ".webm", ".mkv", ".mov"))]
        el = round(time.time() - start, 1)
        log_add("h3", "视频完成，用时 %.1fs，产出 %d 个文件" % (el, len(files)), "k")
        return {"ok": True, "mode": "comfy", "engine": H3_COMFY_URL, "prompt_id": pid,
                "elapsed": el, "info": info,
                "videos": videos, "files": files, "archived": archived,
                "status": st.get("status_str")}
    log_add("h3", "视频生成超时（prompt_id=%s）" % str(pid)[:8], "e")
    return {"ok": False, "error": "H3 生成超时", "prompt_id": pid, "info": info}


def run_h3(opts):
    # H3 走 ComfyUI（默认就是 8188 那个实例，用原生 MiniMax-H3 节点 + CreateVideo/SaveVideo）
    if (H3.get("mode") or "comfy") == "comfy":
        s, _ = h3_api("/queue", timeout=6)
        if s != 200:
            return {"ok": False, "mode": "comfy", "engine": H3_COMFY_URL,
                    "error": "ComfyUI 引擎 %s 不可达。请先在顶栏点「H3 视频」或「ComfyUI 绘图」把它启动。" % H3_COMFY_URL}
        job_begin(H3_COMFY_URL, "h3 · " + (opts.get("unet_name") or "MiniMax-H3"))
        r = {}
        try:
            r = h3_run(opts)
            return r
        finally:
            job_end(str((r or {}).get("error") or ""), bool((r or {}).get("ok")))
    endpoint = (opts.get("endpoint") or H3.get("endpoint", "")).rstrip("/")
    mode = opts.get("mode") or H3.get("mode", "local")
    api_key = opts.get("api_key") or H3.get("api_key", "")

    # 组装旧整合包风格的 H3 设置（六段式提示词 + 参考图 + 参数 + 模型路径）
    payload = {
        "prompt": opts.get("prompt", ""),
        "mode": opts.get("h3mode") or "reference",
        "steps": int(opts.get("steps", 10)),
        "seed": int(opts.get("seed", -1)),
        "duration": int(opts.get("duration", 10)),
        "fps": int(opts.get("fps", 24)),
        "stage1_w": int(opts.get("stage1_w", 1024)),
        "stage1_h": int(opts.get("stage1_h", 780)),
        "stage2_w": int(opts.get("stage2_w", 1024)),
        "stage2_h": int(opts.get("stage2_h", 780)),
        "unet_name": H3.get("model_path", ""),
        "loras": [{"path": H3.get("lora_path", ""), "strength": 1.0}] if H3.get("lora_path") else [],
        "te_path": H3.get("te_path", ""),
        "vae_video_path": H3.get("vae_video_path", ""),
        "vae_audio_path": H3.get("vae_audio_path", ""),
        "img_paths": opts.get("refs", []),
        "audio_paths": [],
        "video_paths": [],
    }

    if not endpoint:
        return {"ok": False,
                "error": "未配置 H3 端点。提示：本机 ComfyUI(8188) 模式已在 16GB 显存上真跑通 H3"
                         "（实测峰值约 14GB，见 2026-09-23 验收报告）；只有改用远程 llama-server"
                         " 端点模式才需要显存≥48GB 的机器，并在此处填其地址（如 http://192.168.x.x:8080）。"}

    if mode == "cloud":
        url = "https://api.minimax.io/v1/video_generation"
        headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
        data = {"model": "MiniMax-H3", "prompt": payload["prompt"]}
        try:
            res = http_json(url, data, timeout=120, headers=headers)
        except Exception as e:
            return {"ok": False, "error": "MiniMax 云端请求失败：%r" % (e,)}
        return {"ok": True, "mode": "cloud", "raw": res}
    else:
        url = endpoint + "/v1/h3"
        try:
            res = http_json(url, payload, timeout=120)
        except Exception as e:
            return {"ok": False,
                    "error": "本地 H3 端点不可达：%s\n请确认那台机器上的 llama-server 已启动且地址正确。"
                             % (e,)}
        # 若返回了 job id，按配置轮询
        job_id = res.get("id") or res.get("job_id") or (res.get("data") or {}).get("task_id")
        if job_id:
            pat = H3.get("job_status_url", "{{base}}/v1/h3/{{id}}")
            status_url = pat.replace("{{base}}", endpoint).replace("{{id}}", str(job_id))
            for _ in range(int(H3.get("max_poll", 180))):
                time.sleep(float(H3.get("poll_seconds", 10)))
                try:
                    st = http_json(status_url, timeout=60)
                except Exception:
                    st = {}
                if st.get("status") in ("success", "done", "completed") or st.get("video_url"):
                    return {"ok": True, "mode": "local", "job_id": job_id, "result": st}
                if st.get("status") in ("error", "failed"):
                    return {"ok": False, "error": "H3 任务失败", "detail": st}
            return {"ok": True, "mode": "local", "job_id": job_id, "result": res,
                    "note": "已提交并轮询超时，请到 H3 服务端查看结果。"}
        return {"ok": True, "mode": "local", "result": res}


# --------------------------------------------------------------------------- #
# 模型注册表 / 标签库 / H3 模板 / 提示词优化器 / 输出归档
# --------------------------------------------------------------------------- #
def _arch_disabled(arch):
    """架构被标记 disabled（如缺失专用 TE）时不对外提供选择。"""
    return bool((ARCH_WF.get(arch) or {}).get("disabled"))


def models_for_mode(mode):
    """按模式门禁返回可用底模/LoRA（对标旧整合包的 _allowed_archs_for_mode）。"""
    allowed = MODE_ARCHS.get(mode) or []
    def pick(items):
        out = []
        for m in items or []:
            a = m.get("arch")
            if (not allowed or a in allowed) and not _arch_disabled(a):
                out.append(m)
        return out
    return {"unets": pick(MODELS.get("unets")),
            "loras": list(MODELS.get("loras") or []),   # LoRA 不按模式过滤：导入即可选用
            "allowed_archs": allowed}


def load_tags():
    try:
        with open(TAG_LIBRARY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_h3_templates():
    """H3 提示词模板（h3_general + 各场景 guide），取自参考包的 prompt_guides。"""
    items = []
    try:
        with open(OPT.get("guide_manifest", ""), "r", encoding="utf-8") as f:
            m = json.load(f)
        g = m.get("general") or {}
        if g:
            items.append({"id": g.get("id", "h3_general"), "name": g.get("name", "H3 General"),
                          "name_zh": g.get("name_zh", "H3 通用"), "path": g.get("path", "")})
        for s in m.get("scene_guides") or []:
            if s.get("path"):
                items.append({"id": s.get("id"), "name": s.get("name"),
                              "name_zh": s.get("name_zh"), "path": s.get("path")})
    except Exception:
        pass
    return {"root": OPT.get("guide_root", ""), "items": items}


def read_guide(rel_path):
    """读 guide/参考文档内容（限制在 guide_root 内，防越界）。"""
    root = os.path.abspath(OPT.get("guide_root") or ".")
    full = os.path.abspath(os.path.join(root, rel_path.replace("/", os.sep)))
    if not full.startswith(root) or not os.path.isfile(full):
        return ""
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[:60000]
    except Exception:
        return ""


def _image_parts(opts):
    """把参考图作为 data URL 塞进 messages（读图需视觉模型）。"""
    out = []
    if not OPT.get("read_media"):
        return out
    for i, r in enumerate((opts.get("refs") or [])[:9]):
        p = r if os.path.isabs(r) else os.path.join(OUTPUT_DIR, r)
        try:
            b = base64.b64encode(open(p, "rb").read()).decode()
            out.append({"type": "text", "text": "<Picture %d> 参考图：" % (i + 1)})
            out.append({"type": "image_url", "image_url": {"url": "data:image/png;base64," + b}})
        except Exception:
            pass
    return out


def build_optimize_messages(opts):
    """按任务分派：
       mode=h3           → H3 六段式提示词（通用 guide + base/ref + 所选场景 guide）
       mode=image        → 文生图/图生图「结构化描述」（中文 → 结构化英文 PROMPT/NEGATIVE）
       mode=interrogate  → 图片反推描述（读图出提示词）
    """
    mode = (opts.get("mode") or "h3").lower()

    if mode == "interrogate":
        if _LANG == "zh":
            sys_msg = ("你是图像描述专家。仔细看图，输出一条**中文**提示词，用逗号分隔地描述："
                       "主体、外貌/服饰、姿态动作、背景环境、光线、镜头景别、画风。"
                       "只输出提示词本身：不要编号、不要解释、不要换行、不要引号。")
        else:
            sys_msg = ("You are an expert image captioner. Look at the image and output ONE comma-separated "
                       "English prompt describing it in detail (subject, appearance, clothing, pose, "
                       "background, lighting, camera, style). Output ONLY the prompt — no numbering, "
                       "no commentary, no line breaks.")
        content = [{"type": "text", "text": "Describe this image as a generation prompt."}] + _image_parts(opts)
        return [{"role": "system", "content": sys_msg}, {"role": "user", "content": content}]

    if mode == "image":
        if _LANG == "zh":
            sys_msg = ("你是提示词工程师。把用户的想法扩写成一条高质量的扩散模型提示词，"
                       "**必须用中文输出**。只输出下面两行，不要任何多余内容：\n"
                       "PROMPT: <中文提示词，用中文逗号分隔：主体、外貌、服饰、动作姿态、"
                       "环境背景、光线、镜头、景别、画风、质量词>\n"
                       "NEGATIVE: <中文负向提示词，用中文逗号分隔：模糊、低质量、畸形手指等>\n"
                       "不要写成英文，不要加解释，不要加代码块。"
                       "若用户写了 <Picture N> 标签，原样保留。")
        else:
            sys_msg = ("You are a prompt engineer. Turn the user's Chinese idea into ONE high-quality "
                       "diffusion prompt. Output EXACTLY two lines and nothing else:\n"
                       "PROMPT: <comma-separated English prompt: subject, appearance, clothing, action, "
                       "environment, lighting, lens, shot size, style, quality words>\n"
                       "NEGATIVE: <comma-separated English negative prompt>\n"
                       "Keep any <Picture N> tags the user wrote.")
        content = [{"type": "text", "text": (opts.get("prompt") or "").strip() or "(empty)"}]
        content += _image_parts(opts)
        return [{"role": "system", "content": sys_msg}, {"role": "user", "content": content}]

    guide_rel = opts.get("guide") or ""
    zh = _H3_LANG == "zh"
    six = ("subject_definitions:\nsummary:\nretention_analysis:\ndetailed_description:\n"
           "overall_soundscape:\nnon_diegetic_music:\n")
    # ⚠ 坑（复发过）：guide.md 里带 T2VA 的 integrated_multimodal_description 三字段样例，
    #   模型会照抄它。只把格式指令放末尾不够 —— 必须「开头先立规矩 + 末尾再压一次」。
    parts = ["【最终任务】把用户的想法重写成一条 MiniMax H3 提示词。"
             "只能输出且必须输出以下六个字段，按顺序、各占一行：\n" + six +
             ("【语言】六个字段的内容**全部用中文**书写。\n" if zh else "【语言】全部用英文书写。\n") +
             "下面先给你参考资料（仅供学习写法与风格，不要照搬其中的字段名）。\n\n"]
    for label, rel in (("GENERAL GUIDE", "h3_general/guide.md"),
                       ("BASE REFERENCE", "h3_general/references/base-en.txt"),
                       ("REFERENCE REFERENCE", "h3_general/references/ref-en.txt"),
                       ("SCENE GUIDE", guide_rel)):
        if not rel:
            continue
        txt = read_guide(rel)
        if txt:
            parts.append("--- 参考资料：%s ---\n%s\n\n" % (label, txt))
    parts.append(
        "【参考资料结束】现在按【最终任务】输出。硬性规定：\n"
        "1. 只能使用上面那六个字段名；严禁输出 integrated_multimodal_description，"
        "也严禁输出参考资料里出现的任何其它顶层字段名；\n"
        "2. 第一个字段必须是 subject_definitions:，它前面不能有任何文字；\n"
        "3. 六个字段的内容" + ("全部用中文书写（含镜头描述与声音描述）；\n" if zh else "全部用英文书写；\n") +
        "4. 镜头用 [Shot N] 标记，第 2 个及以后的镜头用 At 00:0X.XXX 时间戳；"
        "每个镜头内联该时段的精确声音（标注秒数）；\n"
        "5. 参考素材标记：<Picture N> 图片、<Video N> 视频、<Audio N> 音频；\n"
        "6. 只输出提示词正文，不要解释，不要 markdown 代码块。")
    content = [{"type": "text", "text": (opts.get("prompt") or "").strip() or "(empty)"}] + _image_parts(opts)
    return [{"role": "system", "content": "".join(parts)},
            {"role": "user", "content": content}]


def _opt_providers():
    """可选的优化器引擎（界面上的「优化器选择」分段按钮从这里来）。
    没配 providers 时，退化成单一主端点，行为与以前一致。"""
    ps = [p for p in (OPT.get("providers") or []) if (p.get("api_url") and p.get("model"))]
    if ps:
        return ps
    return [{"id": "default", "name": OPT.get("model") or "默认",
             "api_url": OPT.get("api_url"), "api_key": OPT.get("api_key", ""),
             "model": OPT.get("model")}]


def opt_providers_public():
    """给前端「添加第三方模型」弹窗用的完整列表。

    ⚠ api_key 只回 has_key，不回明文：页面输入框留空即表示「沿用原 key」，
      否则用户一打开弹窗、一保存，没改过的 key 就被空字符串覆盖掉（踩过同类坑）。
    """
    out = []
    for p in (OPT.get("providers") or []):
        out.append({"id": p.get("id") or "", "name": p.get("name") or "",
                    "api_url": p.get("api_url") or "", "model": p.get("model") or "",
                    "has_key": bool(p.get("api_key"))})
    return out


def _opt_prov_slug(name):
    base = re.sub(r"[^0-9A-Za-z]+", "-", (name or "").strip()).strip("-").lower()
    return base or ("custom-%s" % uuid.uuid4().hex[:8])


def save_opt_provider(p):
    """新增/更新一个优化器引擎（第三方 OpenAI 兼容模型），写进 prompt_optimizer.providers。"""
    url = (p.get("api_url") or "").strip()
    model = (p.get("model") or "").strip()
    if not url or not model:
        return False, "接口地址和模型名都不能为空", ""
    name = (p.get("name") or "").strip() or model
    ps = list(OPT.get("providers") or [])
    pid = (p.get("id") or "").strip()
    if not pid:
        pid = _opt_prov_slug(name)
        while any((x.get("id") or "") == pid for x in ps):
            pid = pid + "-" + uuid.uuid4().hex[:4]
    cur = None
    for x in ps:
        if (x.get("id") or "") == pid:
            cur = x
            break
    if cur is None:
        cur = {"id": pid}
        ps.append(cur)
    cur["name"] = name
    cur["api_url"] = url
    cur["model"] = model
    if p.get("api_key"):
        cur["api_key"] = p["api_key"]
    OPT["providers"] = ps
    ok, err = save_config()
    return ok, err, pid


def delete_opt_provider(pid):
    """删除一个优化器引擎；至少要留一个，否则优化器整体不可用。"""
    ps = list(OPT.get("providers") or [])
    left = [x for x in ps if (x.get("id") or "") != pid]
    if len(left) == len(ps):
        return False, "没找到这个引擎（id=%s）" % pid
    if not left and not (OPT.get("api_url") and OPT.get("model")):
        return False, "至少要保留一个优化器引擎"
    OPT["providers"] = left
    return save_config()


def opt_remote_models(url, api_key=""):
    """拉 OpenAI 兼容端点的 /v1/models，让弹窗里的模型名可以直接选。

    地址三种写法都兼容：.../v1、.../v1/chat/completions、.../chat/completions。"""
    import urllib.request
    u = (url or "").rstrip("/")
    for suf in ("/chat/completions",):
        if u.endswith(suf):
            u = u[: -len(suf)]
    if not u.endswith("/v1"):
        u += "/v1"
    hdrs = {"User-Agent": OPT.get("user_agent") or
                          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
    if api_key:
        hdrs["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(u + "/models", headers=hdrs)
    with urllib.request.urlopen(req, timeout=15) as r:
        j = json.loads(r.read().decode("utf-8", "replace") or "{}")
    return [x.get("id") for x in (j.get("data") or []) if x.get("id")]


def _opt_saved(pid):
    for x in (OPT.get("providers") or []):
        if (x.get("id") or "") == pid:
            return x
    return None


def test_opt_provider(p):
    """真发一条最小请求验证能不能用（不猜、不靠 GET 元数据接口——那条只能给假设）。"""
    import urllib.request
    url = (p.get("api_url") or "").strip()
    model = (p.get("model") or "").strip()
    if not url or not model:
        return {"ok": False, "error": "接口地址和模型名都不能为空"}
    # ⚠ 编辑已有引擎时前端拿不到明文 key（后端只回 has_key）→ 回退到已存的那份，
    #   否则「没改 key 点测试」必然 401，会把好引擎误判成坏的。
    if not p.get("api_key") and p.get("id"):
        saved = _opt_saved(p["id"]) or {}
        p = dict(p)
        p["api_key"] = saved.get("api_key") or ""
    body = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": "reply with: ok"}],
                       "max_tokens": 16}).encode("utf-8")
    hdrs = {"Content-Type": "application/json",
            # ⚠ 同 call_optimizer：Cloudflare 会把 Python 默认 UA 判成 1010 挡掉
            "User-Agent": OPT.get("user_agent") or
                          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
    if p.get("api_key"):
        hdrs["Authorization"] = "Bearer " + p["api_key"]
    try:
        req = urllib.request.Request(_chat_url(url), data=body, headers=hdrs)
        with urllib.request.urlopen(req, timeout=min(int(OPT.get("timeout", 240)), 60)) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        txt = d["choices"][0]["message"]["content"]
        if isinstance(txt, list):
            txt = "".join(x.get("text", "") for x in txt)
        models = []
        try:
            models = opt_remote_models(url, p.get("api_key") or "")
        except Exception:
            pass
        return {"ok": True, "reply": (txt or "").strip()[:80], "models": models,
                "model": model}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _chat_url(u):
    """兼容两种写法：给了完整 .../v1/chat/completions 就直接用，只给 /v1 就补上。"""
    u = (u or "").rstrip("/")
    return u if u.endswith("/chat/completions") else u + "/chat/completions"


def call_optimizer(opts):
    """调 OpenAI 兼容接口做提示词优化。
    opts["provider"] 指定用哪个引擎（界面分段选择器传来的 id），失败才依次回落其它引擎。"""
    import urllib.request
    ps = _opt_providers()
    want = (opts.get("provider") or "").strip()
    cands, seen = [], set()
    for p in ([x for x in ps if (x.get("id") or "") == want] +
              [x for x in ps if (x.get("id") or "") != want]):
        key = (p.get("api_url"), p.get("model"))
        if key in seen:
            continue
        seen.add(key)
        cands.append(p)
    for p in (OPT.get("fallbacks") or []):
        key = (p.get("api_url"), p.get("model"))
        if key not in seen:
            seen.add(key)
            cands.append(p)
    msgs = build_optimize_messages(opts)
    last = "未配置端点"
    for c in cands:
        url = (c.get("api_url") or "")
        if not url:
            continue
        body = json.dumps({"model": c.get("model"), "messages": msgs,
                           "temperature": 0.3, "max_tokens": 8000}).encode("utf-8")
        hdrs = {"Content-Type": "application/json",
                # ⚠ 坑：urllib 默认 UA 是 "Python-urllib/3.x"，Cloudflare 会直接判为
                #    ``error code: 1010`` 挡掉（实测 某个 Cloudflare 网关 就挡这个，curl 却正常）。
                #    OpenAI 兼容请求必须伪装成正常客户端 UA。
                "User-Agent": OPT.get("user_agent") or
                              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}
        if c.get("api_key"):
            hdrs["Authorization"] = "Bearer " + c["api_key"]
        try:
            req = urllib.request.Request(_chat_url(url), data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=int(OPT.get("timeout", 240))) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            txt = d["choices"][0]["message"]["content"]
            if isinstance(txt, list):
                txt = "".join(x.get("text", "") for x in txt)
            return {"ok": True, "prompt": (txt or "").strip(), "endpoint": url,
                    "model": c.get("model"), "provider": c.get("id") or "",
                    "provider_name": c.get("name") or c.get("model") or "",
                    "fell_back": bool(want) and (c.get("id") or "") != want}
        except Exception as e:
            last = "%s (%s) -> %s" % (url, c.get("model"), e)
    return {"ok": False, "error": "提示词优化失败：%s" % last}


def archive_outputs(module, model_name, paths):
    """归档到 <archive_root>/<模块>/<模型>/<日期>/（对标参考包的输出归档结构）。
    ⚠ 坑：同名文件可能来自「另一个引擎 / 引擎重启后计数归零」的不同图，
      所以不能只判存在就跳过，否则归档里会留着一张旧图（踩过）。
      内容不同 → 存成 name__2.ext，两份都保留。"""
    if not ARCHIVE_BY_MODULE or not ARCHIVE_ROOT:
        return []
    import shutil
    ok = []
    day = time.strftime("%Y-%m-%d")
    safe_model = "".join(ch for ch in (model_name or "unknown") if ch not in '\\/:*?"<>|').strip() or "unknown"
    d = os.path.join(ARCHIVE_ROOT, module, safe_model, day)
    try:
        os.makedirs(d, exist_ok=True)
        for p in paths:
            if not os.path.isfile(p):
                continue
            stem, ext = os.path.splitext(os.path.basename(p))
            dst = os.path.join(d, stem + ext)
            n = 2
            while os.path.exists(dst):
                try:
                    if os.path.getsize(dst) == os.path.getsize(p):
                        break            # 同内容，已归档过
                except OSError:
                    pass
                dst = os.path.join(d, "%s__%d%s" % (stem, n, ext))
                n += 1
            if not os.path.exists(dst):
                shutil.copy2(p, dst)
            ok.append(dst)
    except Exception:
        pass
    return ok


_SYS_PREV = {"idle": 0, "total": 0}


def sysmetrics():
    """CPU / 内存 / 磁盘占用（纯 ctypes，零依赖）；GPU 显存由前端另取 /api/status。"""
    import ctypes
    out = {}

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    def ft(v):
        return (v.high << 32) | v.low

    try:
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        if ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            i, t = ft(idle), ft(kern) + ft(user)
            di, dt = i - _SYS_PREV["idle"], t - _SYS_PREV["total"]
            _SYS_PREV.update({"idle": i, "total": t})
            if dt > 0:
                out["cpu_percent"] = round(100.0 * (1 - di / dt), 1)
    except Exception:
        pass
    try:
        class MemEx(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64)]
        m = MemEx()
        m.dwLength = ctypes.sizeof(MemEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            out["ram_used_gb"] = round((m.ullTotalPhys - m.ullAvailPhys) / 1e9, 1)
            out["ram_total_gb"] = round(m.ullTotalPhys / 1e9, 1)
            out["ram_percent"] = m.dwMemoryLoad
    except Exception:
        pass
    try:
        free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        drive = os.path.splitdrive(os.path.abspath(OUTPUT_DIR))[0] or "C:"
        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(drive + "\\"), None,
                                                      ctypes.byref(total), ctypes.byref(free)):
            out["disk_free_gb"] = round(free.value / 1e9, 1)
            out["disk_total_gb"] = round(total.value / 1e9, 1)
            out["disk_drive"] = drive
    except Exception:
        pass
    return out


def save_config():
    """把内存里的 MODELS 写回 config.json。"""
    try:
        CONFIG["models"] = MODELS
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, ensure_ascii=False, indent=2)
        return True, ""
    except Exception as e:
        return False, str(e)


def model_dir_for(kind):
    """取该类别第一个存在的 ComfyUI 模型目录。"""
    for d in ((CONFIG.get("model_dirs") or {}).get(kind) or []):
        if os.path.isdir(d):
            return d
    return ""


def human_size(n):
    try:
        n = float(n)
    except Exception:
        return ""
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.2f%s" % (n, u)
        n /= 1024.0


# --------------------------------------------------------------------------- #
# LoRA ↔ 底模 兼容性预检（读头部做张量级比对）+ 自动注入
#   背景：LoRA 只对「训练它的那个架构」有效。不同架构的层名/隐藏维度不同，
#   硬套上去通常「不报错但完全不生效」（静默失效），所以必须先验证再注入。
# --------------------------------------------------------------------------- #
_SHAPE_CACHE = {}


def _st_read(path):
    """读 safetensors 头部 → (metadata, {张量名: 形状})，不加载权重。"""
    import struct
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        h = json.loads(f.read(n).decode("utf-8", "replace"))
    meta = h.pop("__metadata__", None) or {}
    return meta, {k: tuple(v.get("shape") or []) for k, v in h.items()}


def _gguf_read(path):
    """读 GGUF 张量表 → {张量名: 形状(torch 序)}，不加载权重。"""
    import struct
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            return {}
        struct.unpack("<I", f.read(4))[0]
        nt = struct.unpack("<Q", f.read(8))[0]
        nk = struct.unpack("<Q", f.read(8))[0]

        def rds():
            l = struct.unpack("<Q", f.read(8))[0]
            return f.read(l).decode("utf-8", "replace")

        def rdv(t):
            fmt = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
                   7: "<?", 10: "<Q", 11: "<q", 12: "<d"}.get(t)
            if t == 8:
                return rds()
            if t == 9:
                et = struct.unpack("<I", f.read(4))[0]
                ln = struct.unpack("<Q", f.read(8))[0]
                return [rdv(et) for _ in range(ln)]
            return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]

        for _ in range(nk):
            rds()
            rdv(struct.unpack("<I", f.read(4))[0])
        out = {}
        for _ in range(nt):
            nm = rds()
            nd = struct.unpack("<I", f.read(4))[0]
            dims = [struct.unpack("<Q", f.read(8))[0] for _ in range(nd)]
            struct.unpack("<I", f.read(4))[0]
            struct.unpack("<Q", f.read(8))[0]
            out[nm] = tuple(reversed(dims))
        return out


def _norm_key(k):
    for p in ("model.diffusion_model.", "diffusion_model.", "model."):
        if k.startswith(p):
            return k[len(p):]
    return k


def _cache_key(path):
    try:
        st = os.stat(path)
        return (path, st.st_size, int(st.st_mtime))
    except Exception:
        return (path, 0, 0)


def base_shapes(path):
    """底模的张量名→形状（带缓存）。"""
    key = _cache_key(path)
    if key in _SHAPE_CACHE:
        return _SHAPE_CACHE[key]
    sh = {}
    try:
        raw = _gguf_read(path) if _ext(path) == ".gguf" else _st_read(path)[1]
        sh = {_norm_key(k): v for k, v in raw.items()}
    except Exception:
        sh = {}
    _SHAPE_CACHE[key] = sh
    return sh


def resolve_path(name, kind):
    """模型名 → 真实文件路径：注册表 path 优先，其次模型目录，最后当绝对路径试。"""
    for m in (MODELS.get(kind) or []):
        if m.get("name") == name or os.path.basename(m.get("path") or "") == name:
            p = m.get("path") or ""
            if p and os.path.isfile(p):
                return p
    for d in ((CONFIG.get("model_dirs") or {}).get(kind) or []):
        cand = os.path.join(d, name)
        if os.path.isfile(cand):
            return cand
    if os.path.isabs(name) and os.path.isfile(name):
        return name
    return ""


def lora_visible_name(path):
    """给 ComfyUI 的 lora_name：若该文件已在 loras 目录里就传 basename，
    否则传绝对路径（本机两套引擎实测都能吃绝对路径）。"""
    base = os.path.basename(path)
    for d in ((CONFIG.get("model_dirs") or {}).get("loras") or []):
        if os.path.isfile(os.path.join(d, base)):
            return base
    return path


def _lora_targets(lp):
    """{目标层名: {'A': lora_A 形状, 'B': lora_B 形状}}"""
    d = {}
    for k, shp in _st_read(lp)[1].items():
        m = re.match(r"^(?:diffusion_model\.)?(.*)\.lora_([AB])\.weight$", k)
        if m:
            d.setdefault(m.group(1) + ".weight", {})[m.group(2)] = shp
    return d


def lora_compat(unet_name, lora_specs):
    """逐条给出「该 LoRA 能否用在当前底模」的硬证据。
    判据：同名层数 / 形状真正吻合数（base[out,in] vs A[rank,in] + B[out,rank]）。"""
    upath = resolve_path(unet_name, "unets") if unet_name else ""
    bshapes = base_shapes(upath) if upath else {}
    rep = []
    for spec in (lora_specs or []):
        nm = spec.get("name") if isinstance(spec, dict) else spec
        r = {"name": nm, "ok": False, "matched": 0, "shape_ok": 0, "total": 0,
             "trained_for": "", "reason": ""}
        lp = resolve_path(nm, "loras")
        if not lp:
            r["reason"] = "找不到 LoRA 文件"
            rep.append(r)
            continue
        try:
            tgt = _lora_targets(lp)
            md = _st_read(lp)[0]
        except Exception as e:
            r["reason"] = "LoRA 解析失败：%r" % (e,)
            rep.append(r)
            continue
        r["trained_for"] = (md.get("ss_base_model_version") or md.get("base_model")
                            or md.get("modelspec.architecture") or "")
        r["total"] = len(tgt)
        matched = shape_ok = 0
        for t, ab in tgt.items():
            if t not in bshapes:
                continue
            matched += 1
            A, B = ab.get("A"), ab.get("B")
            bs = bshapes[t]
            if A and B and len(bs) >= 2 and len(A) == 2 and len(B) == 2 \
               and A[1] == bs[1] and B[0] == bs[0] and A[0] == B[1]:
                shape_ok += 1
        r["matched"] = matched
        r["shape_ok"] = shape_ok
        r["ok"] = shape_ok > 0
        if not bshapes:
            r["reason"] = "底模文件未找到或无法解析：%s" % (unet_name or "(空)")
        elif shape_ok == 0:
            r["reason"] = ("与底模 %s 结构不匹配：目标层 %d，同名 %d，形状吻合 0 —— "
                           "该 LoRA 训练于「%s」，套上去不会报错但完全不生效。"
                           % (os.path.basename(upath or unet_name or ""), len(tgt), matched,
                              r["trained_for"] or "未知架构"))
        rep.append(r)
    return rep


def inject_lora(api_prompt, lora_specs, log, unet_name=None):
    """把 LoraLoaderModelOnly 链插在「底模加载器 → 下游」之间。
    不兼容的会跳过并在日志里说明原因（不静默、不白跑一次出图）。"""
    specs = [s for s in (lora_specs or []) if s]
    if not specs:
        return 0
    loaders = [nid for nid, n in (api_prompt or {}).items()
               if n.get("class_type") in ("UnetLoaderGGUF", "UnetLoaderGGUFAdvanced", "UNETLoader")]
    if not loaders:
        log.append("[提示] 图中未找到底模加载节点，LoRA 未注入")
        return 0

    compat = {}
    if unet_name:
        for r in lora_compat(unet_name, specs):
            compat[r["name"]] = r
            if r["ok"]:
                log.append("LoRA 自检通过：%s（%d 层形状吻合，训练于 %s）"
                           % (r["name"], r["shape_ok"], r["trained_for"] or "?"))
            else:
                log.append("[⚠] 跳过 LoRA %s —— %s" % (r["name"], r["reason"]))

    first_loader = loaders[0]
    src, injected = [first_loader, 0], 0
    nid_n = 0
    while ("L%d" % nid_n) in (api_prompt or {}):
        nid_n += 1
    for spec in specs:
        nm = spec.get("name") if isinstance(spec, dict) else spec
        r = compat.get(nm)
        if r is not None and not r["ok"]:
            continue
        lp = resolve_path(nm, "loras")
        if not lp:
            log.append("[⚠] 跳过 LoRA %s：找不到文件" % nm)
            continue
        strength = float(spec.get("weight", 1.0)) if isinstance(spec, dict) else 1.0
        nid = "L%d" % nid_n
        nid_n += 1
        api_prompt[nid] = {"class_type": "LoraLoaderModelOnly",
                           "inputs": {"model": list(src),
                                      "lora_name": lora_visible_name(lp),
                                      "strength_model": strength}}
        log.append("LoRA %s → 节点 %s (LoraLoaderModelOnly x%.2f)" % (os.path.basename(lp), nid, strength))
        src = [nid, 0]
        injected += 1
    if not injected:
        log.append("[⚠] 没有可注入的 LoRA（%d 个全部不兼容/缺失）" % len(specs))
        return 0
    # 把原先直连底模的输入，改接到 LoRA 链尾
    for nid, node in (api_prompt or {}).items():
        if nid == first_loader or str(node.get("class_type") or "").startswith("LoraLoader"):
            continue
        for k, v in list((node.get("inputs") or {}).items()):
            if isinstance(v, list) and len(v) == 2 and str(v[0]) == str(first_loader) and v[1] == 0:
                node["inputs"][k] = list(src)
                log.append("节点%s.%s: 底模 → LoRA 链尾 %s" % (nid, k, src[0]))
    return injected


# --------------------------------------------------------------------------- #
# HTTP 处理器
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静默

    def _send(self, code, obj=None, ctype="application/json; charset=utf-8"):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8") if obj is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _page_route(self):
        """页面注册 / 注销 / 关闭信标。已处理返回 True。

        ⚠ 2026-09-23 修：这三条原先**只挂在 do_POST**，但前端登记页面用的是
        `fetch(url)` —— 默认 method 是 **GET** → 页面从来没被登记过，
        注册表恒为空，守望线程数够 PAGE_EMPTY_HOLD 秒就认定「零页面」，
        会把启动器连同正在跑的引擎一起收掉。现在 do_GET / do_POST 都收：
        sendBeacon（POST）与 fetch（GET）两条路都能用，哪边改了都不至于瞎。
        """
        p = self.path
        if p.startswith("/api/app/pageopen"):
            n = page_open(_qget(p, "pid"))
            self._send(200, {"ok": True, "pages": n})
            return True
        if p.startswith("/api/app/pageclose"):
            n = page_close(_qget(p, "pid"))
            self._send(200, {"ok": True, "pages": n})
            return True
        if p.startswith("/api/app/pagehide"):
            # 老缓存页的兼容路径：它不带 pid，只发这一枪。
            hb_left()
            page_close("__anon__")
            self._send(200, {"ok": True, "left": hb_left_age(), "pages": pages_alive()})
            return True
        return False

    def _send_file(self, path):
        if not os.path.isfile(path):
            self._send(404, {"error": "not found"})
            return
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "webp": "image/webp", "gif": "image/gif", "mp4": "video/mp4",
                 "webm": "video/webm",
                 # 页面/静态资源：缺这几项会让浏览器把 HTML 当二进制下载（而不是渲染），
                 # 表现为打开 http://127.0.0.1:8777 直接弹下载、看不到界面。
                 "html": "text/html; charset=utf-8", "htm": "text/html; charset=utf-8",
                 "css": "text/css; charset=utf-8", "js": "application/javascript",
                 "json": "application/json; charset=utf-8", "svg": "image/svg+xml",
                 "ico": "image/x-icon", "txt": "text/plain; charset=utf-8",
                 }.get(ext, "application/octet-stream")
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            self._send(500, {"error": str(e)})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    # ================= GET 路由分发 =================
    # 所有「读」请求入口：首页/静态文件、状态(/api/init、/api/status)、
    # 模型列表、配置读取、引擎状态、对话历史、H3 任务查询、优化器 providers 等。
    # 约定：路径带查询串(?x=1)时按「? 之前」部分匹配，避免手抄带参链接变白页。
    def do_GET(self):
        # ⚠ 用 .split("?")[0] 比 self.path in ("/", ...) 稳：带任何查询串
        #   （?utm=… / ?diag=1 / 用户手抄链接带参数）以前一律 404 → 打开是白页，
        #   看着像"服务挂了"。路径部分匹配上就给首页。
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            html = os.path.join(HERE, "comfy_studio.html")
            if os.path.isfile(html):
                self._send_file(html)
            else:
                self._send(404, {"error": "前端 comfy_studio.html 缺失"})
            return
        if self.path.startswith("/outputs/"):
            rest = self.path[len("/outputs/"):]
            idx, rel = 0, rest
            head, sep, tail = rest.partition("/")
            if sep and head.isdigit():
                idx, rel = int(head), tail
            rel = rel.replace("/", os.sep)
            order = ([OUTPUT_DIRS[idx]] if 0 <= idx < len(OUTPUT_DIRS) else []) + OUTPUT_DIRS
            for od in order:
                cand = os.path.join(od, rel)
                if os.path.isfile(cand):
                    self._send_file(cand)
                    return
            self._send(404, {"error": "not found"})
            return
        if self.path.startswith("/h3out/"):
            rel = os.path.basename(self.path[len("/h3out/"):])
            self._send_file(os.path.join(H3_OUT_DIR, rel))
            return
        # 进度 + 日志合成一个口：前端只轮询它，拿「进度快照 + 增量日志行」
        if self.path.startswith("/api/progress"):
            try:
                since = int(_qget(self.path, "since") or 0)
            except Exception:
                since = 0
            snap = prog_snapshot()
            pub = log_public(since)
            self._send(200, {"ok": True, "progress": snap, "seq": pub["seq"],
                             "rows": pub["rows"], "logfile": pub["file"],
                             "logdir": pub["dir"]})
            return
        # 全量日志（左栏子栏首次打开 / 「复制」「导出」用）
        if self.path.startswith("/api/log"):
            try:
                n = int(_qget(self.path, "n") or 900)
            except Exception:
                n = 900
            snap = prog_snapshot()
            pub = log_public(0, n)
            self._send(200, {"ok": True, "seq": pub["seq"], "rows": pub["rows"],
                             "file": pub["file"], "dir": pub["dir"],
                             "tags": list(LOG_TAGS), "hist": snap.get("hist") or {},
                             "progress": snap})
            return
        if self.path.startswith("/api/engine/status"):
            m = _engine_manager()
            if m is None:
                self._send(500, {"error": "comfy_engine 未加载"})
                return
            self._send(200, m.status_all(deep=("deep=1" in self.path)))
            return
        if self.path.startswith("/api/engine/detect"):
            eid = "comfy"
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("id="):
                        eid = urllib_parse.unquote(kv[3:])
            m = _engine_manager()
            self._send(200, m.detect(eid) if m else {"error": "comfy_engine 未加载"})
            return
        if self.path.startswith("/api/engine/log"):
            eid, n = "llm", 120
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("id="):
                        eid = urllib_parse.unquote(kv[3:])
                    elif kv.startswith("n="):
                        try:
                            n = int(kv[2:])
                        except Exception:
                            pass
            m = _engine_manager()
            self._send(200, m.log_tail(eid, n) if m else {"ok": False, "error": "comfy_engine 未加载"})
            return
        if self.path.startswith("/api/engine/models"):
            eid = "llm"
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("id="):
                        eid = urllib_parse.unquote(kv[3:])
            m = _engine_manager()
            d = (m.defn(eid) if m else {}) or {}
            self._send(200, {
                "ok": True,
                "models": _ceng.scan_models(d) if (m and _ceng) else [],
                "active": _ceng.llm_active_model(d) if (m and _ceng) else "",
                "vram_est_gb": _ceng.llm_vram_est(d) if (m and _ceng and eid == "llm") else None,
            })
            return
        if self.path == "/api/settings":
            self._send(200, settings_public())
            return
        if self.path.startswith("/api/chat/targets"):
            self._send(200, {"targets": chat_targets_public()})
            return
        if self.path.startswith("/api/chat/models"):
            self._send(200, chat_models())
            return
        # ---- 优化器「添加第三方模型」弹窗用 ----
        if self.path.startswith("/api/optimizer/providers"):
            self._send(200, {"providers": opt_providers_public()})
            return
        if self.path.startswith("/api/optimizer/models"):
            url = urllib_parse.unquote(_qget(self.path, "url"))
            key = urllib_parse.unquote(_qget(self.path, "key"))
            if not key:                       # 同上：编辑态沿用已存的 key
                key = (_opt_saved(urllib_parse.unquote(_qget(self.path, "id"))) or {}).get("api_key") or ""
            try:
                self._send(200, {"ok": True, "models": opt_remote_models(url, key)})
            except Exception as e:
                # 拉不到模型列表不影响手填，所以这里回 200 + ok=False，不当错误码处理
                self._send(200, {"ok": False, "models": [],
                                 "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
            return
        if self.path.startswith("/api/store"):
            key = ""
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("key="):
                        key = urllib_parse.unquote(kv[4:])
            if not key:
                self._send(400, {"error": "需要 key"})
                return
            self._send(200, {"key": key, "value": store_get(key)})
            return
        # ---- 调试接口（CDP）发现：启动器把 Chrome 实际调试端口写在 <data>/browser_cdp.json ----
        #      外部工具（AI / 自动化脚本）先问这里，就知道该连哪个端口接管本页面。
        if self.path.startswith("/api/app/cdp"):
            import urllib.request as _ur
            info = {}
            try:
                with open(os.path.join(DATA_DIR, "browser_cdp.json"), "r", encoding="utf-8") as f:
                    info = json.load(f) or {}
            except Exception:
                info = {}
            port = int(info.get("port") or 0)
            alive = False
            if port:
                try:
                    with _ur.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=2) as r:
                        alive = r.status == 200
                except Exception:
                    alive = False
            self._send(200, {"ok": bool(port), "alive": alive, **info})
            return
        if self.path == "/api/paths":
            self._send(200, {"output_dirs": OUTPUT_DIRS, "output_dir": OUTPUT_DIR,
                             "archive_root": ARCHIVE_ROOT, "archive_by_module": ARCHIVE_BY_MODULE,
                             "out_dir": OUT_DIR, "h3_out_dir": H3_OUT_DIR})
            return
        if self.path == "/api/status":
            import urllib.request
            h3_online = False
            if H3_COMFY_URL:
                try:
                    with urllib.request.urlopen(H3_COMFY_URL + "/queue", timeout=4) as r:
                        h3_online = r.status == 200
                except Exception:
                    h3_online = False
            try:
                d = json.load(urllib.request.urlopen(COMFY_URL + "/system_stats", timeout=6))
                devs = [{"name": x.get("name"),
                         "vram_free_gb": round((x.get("vram_free") or 0) / 1e9, 1),
                         "vram_total_gb": round((x.get("vram_total") or 0) / 1e9, 1)}
                        for x in d.get("devices", [])]
                self._send(200, {"comfy_online": True, "comfy_url": COMFY_URL, "devices": devs,
                                 "h3_online": h3_online, "h3_url": H3_COMFY_URL})
            except Exception as e:
                self._send(200, {"comfy_online": False, "comfy_url": COMFY_URL, "error": str(e),
                                 "h3_online": h3_online, "h3_url": H3_COMFY_URL})
            return
        if self.path == "/api/h3/nodes":
            # 供前端/调试：确认 H3 引擎在线及其 H3 节点
            import urllib.request as _u
            try:
                with _u.urlopen(H3_COMFY_URL + "/object_info", timeout=30) as r:
                    info = json.loads(r.read().decode("utf-8", "replace"))
                keys = [k for k in info if ("MiniMaxH3" in k or "MMH3" in k or "Minimax" in k)]
                self._send(200, {"ok": True, "url": H3_COMFY_URL, "total_nodes": len(info), "h3_nodes": sorted(keys)})
            except Exception as e:
                self._send(200, {"ok": False, "url": H3_COMFY_URL, "error": str(e)})
            return
        if self.path == "/api/gallery":
            items = []
            for od in OUTPUT_DIRS:
                if not os.path.isdir(od):
                    continue
                for root, _, files in os.walk(od):
                    for f in files:
                        if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm")):
                            p = os.path.join(root, f)
                            try:
                                items.append({"url": url_for(p), "dir": od,
                                              "mtime": os.path.getmtime(p)})
                            except Exception:
                                pass
            items.sort(key=lambda x: x["mtime"], reverse=True)
            self._send(200, {"images": items[:60]})
            return
        if self.path == "/api/models":
            mode = "txt2img"
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("mode="):
                        mode = kv[5:]
            self._send(200, models_for_mode(mode))
            return
        if self.path == "/api/models/meta":
            self._send(200, {"arch_options": CONFIG.get("arch_options") or [],
                             "model_dirs": CONFIG.get("model_dirs") or {},
                             "dirs_ok": {k: model_dir_for(k) for k in ("unets", "loras")}})
            return
        if self.path.startswith("/api/lora/check"):
            # 兼容性自检：?unet=<底模名>（可选 &lora=<LoRA名> 只查一条）
            unet = lora = ""
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("unet="):
                        unet = urllib_parse.unquote(kv[5:])
                    elif kv.startswith("lora="):
                        lora = urllib_parse.unquote(kv[5:])
            specs = [{"name": lora}] if lora else (MODELS.get("loras") or [])
            self._send(200, {"unet": unet, "unet_path": resolve_path(unet, "unets") if unet else "",
                             "reports": lora_compat(unet, specs)})
            return
        if self.path == "/api/tags":
            self._send(200, load_tags())
            return
        if self.path == "/api/h3/templates":
            self._send(200, load_h3_templates())
            return
        if self.path.startswith("/api/heartbeat"):
            # peek=1 → 只读，不刷新任何时间戳。
            # 启动器判断「窗口是不是真关了」时必须用 peek，否则自己把 age 归零，
            # 永远得出「页面还活着」的结论（关窗后引擎会一直挂着不退出）。
            # pid=<页面id> → 给这个页面续期（多标签页各自记账，见 pages_alive()）。
            # 带 pid 的新页面走注册表；老缓存页（不带 pid）退化成 __anon__ 一条，
            # 行为等同改造前（只靠 HB_TIMEOUT），不会因为注册表为空被误杀。
            if "peek=1" not in self.path:
                hb_touch()
                _pid = _qget(self.path, "pid")
                if _pid:
                    page_seen(_pid)
                else:
                    page_seen("__anon__")
            self._send(200, {"ok": True, **heartbeat_state()})
            return
        # 页面注册/注销也走 GET（前端 fetch 默认就是 GET，见 _page_route 注释）
        if self.path.startswith("/api/app/page"):
            if self._page_route():
                return
        if self.path == "/api/init":
            self._send(200, {
                "defaults": DEFAULTS,
                "negative_default": NEGATIVE_DEFAULT,
                "models": {"txt2img": models_for_mode("txt2img"),
                           "img2img": models_for_mode("img2img"),
                           "h3": models_for_mode("h3")},
                "tags": load_tags(),
                "h3_templates": load_h3_templates(),
                "groups": CONFIG.get("groups") or [],
                "app": {"heartbeat": AUTO_EXIT, "hb_timeout": HB_TIMEOUT},
                "optimizer": {"enabled": bool(OPT.get("enabled")),
                              "api_url": OPT.get("api_url"), "model": OPT.get("model"),
                              "providers": [{"id": p.get("id"), "name": p.get("name"),
                                             "model": p.get("model")} for p in _opt_providers()]},
            })
            return
        if self.path == "/api/sys":
            self._send(200, sysmetrics())
            return
        self._send(404, {"error": "not found"})

    # ================= POST 路由分发 =================
    # 所有「写/动作」请求入口。注意：POST 有一张精确路径白名单（见下方
    # _POST_ROUTES 或 self.path 匹配分支），不在白名单里的路径直接 404，
    # 新增接口务必在此登记，否则前端调用会 404（这是之前踩过的坑）。
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path == "/api/upload":
            # base64 JSON: {"name":..., "data": "base64..."}
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
                data = base64.b64decode(obj.get("data", ""))
                name = obj.get("name") or ("upload_%s.png" % uuid.uuid4().hex)
                base = H3_COMFY_URL if (obj.get("engine") == "h3" and H3_COMFY_URL) else None
                fname = upload_to_comfy(name, data, obj.get("subfolder", ""), base)
                self._send(200, {"ok": True, "name": fname})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/cancel":
            try:
                log_add("task", "用户点了「暂停生成」", "w")
                self._send(200, request_cancel())
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
            return
        # 前端把自己的动作（改参数、切模型、清空对话…）也写进同一条日志
        if self.path == "/api/log":
            try:
                o = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                lvl = o.get("lvl") or "i"
                tag = o.get("tag") or "ui"
                if lvl not in ("i", "k", "w", "e"):
                    lvl = "i"
                if tag not in LOG_TAGS:
                    tag = "ui"
                seq = log_add(tag, o.get("msg") or "", lvl)
                self._send(200, {"ok": True, "seq": seq})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/log/clear":
            with LOG_LOCK:
                del LOG_RING[:]
                LOG_SEQ[0] += 1
                LOG_RING.append({"seq": LOG_SEQ[0], "t": time.time(),
                                 "ts": time.strftime("%H:%M:%S"), "lvl": "w",
                                 "tag": "ui", "msg": "—— 日志缓冲已清空（磁盘文件保留）——"})
                seq = LOG_SEQ[0]
            self._send(200, {"ok": True, "seq": seq})
            return
        if self.path == "/api/reveal":
            try:
                o = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                kind = (o.get("kind") or "").lower()
                mod = (o.get("module") or "").strip()
                kmap = {"output": OUTPUT_DIR, "archive": ARCHIVE_ROOT,
                        "out": OUT_DIR, "h3": H3_OUT_DIR}
                p = (kmap.get(kind) or "") if kind else (o.get("path") or o.get("dir") or "")
                if kind == "archive" and mod and ARCHIVE_ROOT:
                    p = os.path.join(ARCHIVE_ROOT, mod)
                self._send(200, reveal_path(p, bool(o.get("select"))))
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/generate":
            try:
                opts = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:
                self._send(400, {"ok": False, "error": "JSON 解析失败：%s" % e})
                return
            mode = opts.get("mode", "txt2img")
            result = run_image(mode, opts)
            # 用户主动暂停不算错误 → 走 200，前端靠 cancelled 字段识别
            self._send(200 if (result.get("ok") or result.get("cancelled")) else 500, result)
            return
        if self.path == "/api/h3":
            try:
                opts = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:
                self._send(400, {"ok": False, "error": "JSON 解析失败：%s" % e})
                return
            # H3 可能耗时很久，用线程跑，先返回 accepted
            holder = {}
            def _worker():
                holder["r"] = run_h3(opts)
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            # 简单同步等（前端用进度提示）；真·长时间会让连接挂起，UI 有超时处理
            t.join(timeout=3600)
            r = holder.get("r", {"ok": False, "error": "无返回"})
            self._send(200 if (r.get("ok") or r.get("cancelled")) else 500, r)
            return
        if self.path == "/api/optimize":
            try:
                opts = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:
                self._send(400, {"ok": False, "error": "JSON 解析失败：%s" % e})
                return
            self._send(200, call_optimizer(opts))
            return
        if self.path == "/api/models/import":
            try:
                o = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:
                self._send(400, {"ok": False, "error": "JSON 解析失败：%s" % e})
                return
            kind = o.get("kind")
            if kind not in ("unets", "loras"):
                self._send(400, {"ok": False, "error": "kind 必须是 unets 或 loras"})
                return
            arch = (o.get("arch") or "未分类").strip()
            target_dir = model_dir_for(kind)
            steps = []
            try:
                if o.get("data"):                      # 方式B：上传内容 → 写入模型目录
                    if not target_dir:
                        self._send(500, {"ok": False, "error": "未找到可用的 %s 模型目录" % kind})
                        return
                    fname = (o.get("filename") or "").strip() or ("upload_%s" % uuid.uuid4().hex[:8])
                    b = base64.b64decode(o["data"])
                    dst = os.path.join(target_dir, fname)
                    with open(dst, "wb") as f:
                        f.write(b)
                    final_path, name = dst, fname
                    steps.append("上传写入 %s（%.1fMB）" % (dst, len(b) / 1e6))
                else:                                  # 方式A：本地路径 → 硬链接进模型目录
                    src = (o.get("path") or "").strip()
                    if not os.path.isfile(src):
                        self._send(400, {"ok": False, "error": "路径不存在：%s" % src})
                        return
                    name = os.path.basename(src)
                    final_path = src
                    if target_dir:
                        dst = os.path.join(target_dir, name)
                        if os.path.abspath(dst).lower() != os.path.abspath(src).lower():
                            if not os.path.exists(dst):
                                try:
                                    os.link(src, dst)       # 同盘硬链接：零拷贝、秒完成
                                    steps.append("已硬链接到 %s" % dst)
                                except Exception:
                                    import shutil as _sh
                                    _sh.copy2(src, dst)
                                    steps.append("跨盘复制到 %s（硬链接不可用）" % dst)
                            else:
                                steps.append("目标已存在，直接登记 %s" % dst)
                            final_path = dst
                    else:
                        steps.append("未找到模型目录，仅登记原路径（ComfyUI 可能看不到）")
                entry = {"id": uuid.uuid4().hex[:8], "name": name, "arch": arch,
                         "meta": "%s · %s" % (arch, human_size(os.path.getsize(final_path))),
                         "path": final_path}
                MODELS.setdefault(kind, []).append(entry)
                ok, err = save_config()
                self._send(200 if ok else 500,
                           {"ok": ok, "entry": entry, "steps": steps,
                            "error": err and ("配置写入失败：%s" % err)})
            except Exception as e:
                self._send(500, {"ok": False, "error": str(e)})
            return
        if self.path == "/api/models/remove":
            try:
                o = json.loads(raw.decode("utf-8", "replace"))
            except Exception as e:
                self._send(400, {"ok": False, "error": "JSON 解析失败：%s" % e})
                return
            kind, mid = o.get("kind"), o.get("id")
            if kind not in ("unets", "loras") or not mid:
                self._send(400, {"ok": False, "error": "需要 kind 与 id"})
                return
            lst = MODELS.get(kind) or []
            removed = [m for m in lst if m.get("id") == mid]
            MODELS[kind] = [m for m in lst if m.get("id") != mid]
            ok, err = save_config()
            self._send(200 if ok else 500, {"ok": ok, "removed": removed, "error": err})
            return

        # ---- 页面注册 / 注销 / 关闭信标 ----
        # ⚠ 逐个页面记账，不是「一次信标 = 全局死讯」：多标签页时关掉一个
        #   只注销它自己，其它还开着的页面照常让后端活着。
        #   路由本体在 _page_route()，do_GET 也收（fetch 默认 GET / sendBeacon POST）。
        if self._page_route():
            return

        # ---- 引擎控制 / 对话流式 / 持久化（桌面版新增） ----
        # ⚠️ 这张白名单必须同步加新路由：不在里面的 POST 会直接落到末尾的 404，
        #    表现形式是「前端提示 not found」，很容易误以为是前端拼错了 URL。
        if self.path in ("/api/engine/start", "/api/engine/stop", "/api/engine/restart",
                         "/api/engine/stop_all", "/api/engine/register_model", "/api/engine/save",
                         "/api/chat/stream", "/api/chat/save", "/api/settings", "/api/store",
                         "/api/comfy/save",
                         # 优化器「添加第三方模型」弹窗（2026-09-24 新增）
                         "/api/optimizer/providers", "/api/optimizer/test",
                         "/api/app/quit"):
            try:
                o = json.loads(raw.decode("utf-8", "replace")) if raw else {}
            except Exception as e:
                self._send(400, {"ok": False, "error": "JSON 解析失败：%s" % e})
                return
            if self.path == "/api/app/quit":
                self._send(200, {"ok": True, "msg": "正在停止引擎并退出"})
                request_quit("api")
                return
            if self.path == "/api/chat/stream":
                chat_stream(self, o)
                return
            if self.path == "/api/store":
                key = o.get("key")
                if not key:
                    self._send(400, {"ok": False, "error": "需要 key"})
                    return
                ok, err = store_put(key, o.get("value"))
                self._send(200 if ok else 500, {"ok": ok, "error": err})
                return
            if self.path == "/api/settings":
                ok, err = save_app_settings(o)
                self._send(200 if ok else 500, {"ok": ok, "error": err,
                                                "settings": settings_public()})
                return
            if self.path == "/api/comfy/save":
                raw_url = (o.get("comfy_url") or "").strip()
                if raw_url:
                    ok, err = set_comfy_url(unmask_url(raw_url, COMFY_URL))
                    if not ok:
                        self._send(500, {"ok": False, "error": err})
                        return
                if "h3_url" in o:
                    ok2, err2 = set_h3_url(o.get("h3_url") or "")
                    if not ok2:
                        self._send(500, {"ok": False, "error": err2})
                        return
                # 改完立刻真连一次：地址填错当场就能看到，不用等出图失败才发现
                online, err3, devs = False, "", []
                try:
                    st, body = engine_api(COMFY_URL, "/system_stats", timeout=8)
                    online = (st == 200)
                    if online:
                        devs = [d.get("name") for d in (json.loads(body).get("devices") or [])]
                    else:
                        err3 = "HTTP %s %s" % (st, (body or "")[:160])
                except Exception as e:
                    err3 = str(e)
                log_add("cfg", "ComfyUI 连接自检 %s → %s%s"
                        % ("成功" if online else "失败", mask_url(COMFY_URL),
                           "" if online else ("（%s）" % err3[:160])), "k" if online else "w")
                self._send(200, {"ok": True, "url": mask_url(COMFY_URL),
                                 "online": online, "error": err3, "devices": devs,
                                 "settings": settings_public()})
                return
            if self.path == "/api/chat/save":
                ok, err = save_chat_targets(o.get("targets") or {})
                self._send(200 if ok else 500, {"ok": ok, "error": err})
                return
            if self.path.startswith("/api/optimizer/providers"):
                act = (o.get("action") or "upsert").strip()
                pid = ""
                if act == "delete":
                    ok, err = delete_opt_provider((o.get("id") or "").strip())
                    log_add("cfg", "删除优化器引擎 %s → %s"
                            % (o.get("id"), "成功" if ok else ("失败：" + str(err)[:120])),
                            "k" if ok else "e")
                else:
                    ok, err, pid = save_opt_provider(o.get("provider") or {})
                    log_add("cfg", "保存优化器引擎 %s → %s"
                            % (pid, "成功" if ok else ("失败：" + str(err)[:120])),
                            "k" if ok else "e")
                # 新增时 id 是后端生成的 slug，必须回给前端：否则前端只能靠
                # 「列表最后一项」猜新加的是哪个，多引擎时很容易选错。
                self._send(200 if ok else 400,
                           {"ok": ok, "error": err, "id": pid,
                            "providers": opt_providers_public()})
                return
            if self.path.startswith("/api/optimizer/test"):
                r = test_opt_provider(o.get("provider") or o)
                log_add("cfg", "优化器引擎连通性测试 → %s"
                        % ("通过（%s）" % r.get("model") if r.get("ok")
                           else "失败：" + str(r.get("error"))[:160]),
                        "k" if r.get("ok") else "w")
                self._send(200, r)
                return
            m = _engine_manager()
            if m is None:
                self._send(500, {"ok": False, "error": "comfy_engine 未加载"})
                return
            if self.path == "/api/engine/start":
                eid = o.get("id") or "llm"
                log_add("engine", "启动引擎 %s%s…" % (eid, "（强制）" if o.get("force") else ""))
                r = m.start(eid, force=bool(o.get("force")))
                log_add("engine", "启动 %s → %s" % (eid, "成功" if r.get("ok") else
                        ("失败：" + str(r.get("error") or r.get("note") or "")[:200])),
                        "k" if r.get("ok") else "e")
                self._send(200, r)
                return
            if self.path == "/api/engine/stop":
                eid = o.get("id") or "llm"
                log_add("engine", "停止引擎 %s…" % eid)
                r = m.stop(eid)
                log_add("engine", "停止 %s → %s" % (eid, "成功" if r.get("ok") else
                        ("失败：" + str(r.get("error") or "")[:200])), "k" if r.get("ok") else "w")
                self._send(200, r)
                return
            if self.path == "/api/engine/restart":
                eid = o.get("id") or "llm"
                log_add("engine", "重启引擎 %s…" % eid)
                r = m.restart(eid, force=bool(o.get("force")))
                log_add("engine", "重启 %s → %s" % (eid, "成功" if r.get("ok") else
                        ("失败：" + str(r.get("error") or "")[:200])), "k" if r.get("ok") else "e")
                self._send(200, r)
                return
            if self.path == "/api/engine/stop_all":
                log_add("engine", "停止全部引擎…", "w")
                r = m.stop_all()
                log_add("engine", "全部引擎已停：%s" % json.dumps(r, ensure_ascii=False)[:300], "w")
                self._send(200, {"ok": True, "results": r})
                return
            if self.path == "/api/engine/register_model":
                self._send(200, m.register_model(o.get("path") or ""))
                return
            if self.path == "/api/engine/save":
                eng = o.get("engines")
                if not isinstance(eng, dict):
                    self._send(400, {"ok": False, "error": "engines 必须是对象"})
                    return
                # ⚠️ 2026-09-23 修：原来是 CONFIG["engines_launch"].update(eng) —— 这是**替换**，
                # 只传 {"llm":{"spec":""}} 会把 llm 下的 thinking/ctx/ngl 全丢掉（实测踩到）。
                # 改成按引擎**逐键合并**，只覆盖显式传来的字段。
                _el = CONFIG.setdefault("engines_launch", {})
                for k, v in eng.items():
                    if isinstance(v, dict):
                        _el.setdefault(k, {}).update(v)
                    else:
                        _el[k] = v
                for k, v in eng.items():
                    if k in m.engines and isinstance(v, dict):
                        m.engines[k].update(v)
                ok, err = save_config()
                self._send(200 if ok else 500, {"ok": ok, "error": err})
                return
        self._send(404, {"error": "not found"})


# --------------------------------------------------------------------------- #
# 引擎 / 对话 / 持久化（桌面版新增）
# --------------------------------------------------------------------------- #
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _engine_manager():
    return ENGINE_MANAGER or init_engine_manager()


def chat_targets_public():
    """给前端的对话目标列表（不含 api_key，只给 has_key）。"""
    out = []
    for tid, t in (CHAT_TARGETS or {}).items():
        out.append({
            "id": tid, "name": t.get("name") or tid, "group": t.get("group") or "cloud",
            "model": t.get("model"), "engine": t.get("engine") or "",
            "api_url": t.get("api_url"), "has_key": bool(t.get("api_key")),
            "model_locked": bool(t.get("model_locked")),
        })
    return out


def _chat_upstream(target_id):
    """解析对话目标 → 统一的 OpenAI 兼容 chat/completions 地址（Ollama 也走 /v1 兼容层）。"""
    t = (CHAT_TARGETS or {}).get(target_id)
    if not t:
        return None, "未知对话目标：%s" % target_id
    base = (t.get("api_url") or "").rstrip("/")
    if not base:
        return None, "对话目标 %s 未配置 api_url" % target_id
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    return {"target": t, "url": url, "kind": (t.get("kind") or "openai").lower()}, ""


def chat_models():
    """聚合三组模型：本地 llama.cpp（/v1/models）/ Ollama / 云端网关（静态）。"""
    m = _engine_manager()
    groups = []
    # 1) 本地 llama.cpp：模型来自 router 的 /v1/models（含 unloaded 状态）
    lport = 8080
    if m:
        lport = int((m.defn("llm") or {}).get("port") or 8080)
    local_t = None
    for tid, t in (CHAT_TARGETS or {}).items():
        if (t.get("group") or "") == "local":
            local_t = t
            break
    models, online = [], False
    try:
        j = http_json("http://127.0.0.1:%d/v1/models" % lport, timeout=6)
        for x in (j.get("data") or []):
            st = x.get("status")
            if isinstance(st, dict):
                st = st.get("value")
            models.append({"id": x.get("id"), "status": st or ""})
        online = True
    except Exception:
        pass
    # 引擎未启动时，models 为空 → 下拉框会空到只剩 fallback 文案（"未安装模型"的观感）。
    # 用模型库扫描补齐：列出库内实际存在的 gguf（标「未加载」），让用户能直接选。
    ldef = (m.defn("llm") if m else {}) or {}
    if not models and _ceng:
        try:
            for x in _ceng.scan_models(ldef):
                if x.get("exists"):
                    models.append({"id": x["name"], "status": "未加载"})
        except Exception:
            pass
    groups.append({
        "id": "local", "name": (local_t or {}).get("name") or "本地 llama.cpp",
        "engine": "llm", "online": online, "models": models,
        # default_model：配置里存的模型名必须真的在当前列表里（llama 的 alias 改名后
        # 旧短名会残留在这里 → 前端拿它发请求会 404）。对不上就回落第一项自愈。
        "default_model": ((local_t or {}).get("model")
                          if any(x["id"] == (local_t or {}).get("model") for x in models)
                          else (models[0]["id"] if models else "")),
    })
    # 2) Ollama（可选引擎）
    oport = 11434
    if m:
        oport = int((m.defn("ollama") or {}).get("port") or 11434)
    omodels, oonline = [], False
    try:
        j = http_json("http://127.0.0.1:%d/api/tags" % oport, timeout=5)
        omodels = [{"id": x.get("name"), "status": "loaded"} for x in (j.get("models") or []) if x.get("name")]
        oonline = True
    except Exception:
        pass
    if oonline or any((t.get("group") or "") == "ollama" for t in (CHAT_TARGETS or {}).values()):
        groups.append({"id": "ollama", "name": "Ollama 本地", "engine": "ollama",
                       "online": oonline, "models": omodels,
                       "default_model": omodels[0]["id"] if omodels else ""})
    # 3) 云端网关（静态列出，模型名锁定在配置里）
    cloud = []
    for tid, t in (CHAT_TARGETS or {}).items():
        if (t.get("group") or "cloud") == "cloud":
            cloud.append({"id": tid, "name": t.get("name") or tid, "status": "cloud",
                          "model": t.get("model"), "has_key": bool(t.get("api_key"))})
    if cloud:
        groups.append({"id": "cloud", "name": "云端网关", "engine": "", "online": True,
                       "models": [{"id": c["id"], "status": "cloud"} for c in cloud],
                       "detail": cloud, "default_model": cloud[0]["id"]})
    return {"groups": groups}


def deps_status():
    """关键第三方依赖自检。

    `comfy_cli` 是唯一一个「非标准库」运行时依赖：comfy_control.convert_ui_to_api
    要靠它把 ComfyUI 的 UI 工作流转成 API 格式。它在函数内 import，一旦打包漏了
    或 venv 里没装，只有点「生成」的时候才炸，很难查。所以开机先自检一次。
    """
    out = {}
    try:
        from comfy_cli.workflow_to_api import convert_ui_to_api, is_api_format  # noqa: F401
        out["comfy_cli"] = {"ok": True, "detail": "UI→API 转换器可用"}
    except Exception as e:
        out["comfy_cli"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                            "detail": "缺少 comfy-cli，文生图/图生图会失败"}
    return out


def settings_public():
    """设置页要的全部信息：应用行为 + 引擎命令 + 云端目标 + 模型库。"""
    m = _engine_manager()
    app = dict(CONFIG.get("app") or {})
    engs = {}
    if m:
        for eid, d in m.engines.items():
            engs[eid] = {
                "name": d.get("name"), "group": d.get("group"),
                "enabled": d.get("enabled", True),
                "exe": d.get("exe"), "cwd": d.get("cwd"), "args": d.get("args") or [],
                "port": d.get("port"), "vram_est_gb": d.get("vram_est_gb"),
                "router": bool(d.get("router")),
                "models_dir": d.get("models_dir"), "model": d.get("model"),
                "alias": d.get("alias"), "ctx": d.get("ctx"),
                "max_predict": d.get("max_predict"),
                "hint": d.get("hint"),
            }
    ldef = (m.defn("llm") if m else {}) or {}
    return {
        "app": app,
        "exclusive": (m.exclusive if m else app.get("exclusive") or "all"),
        "home_dir": HOME_DIR,
        "config_path": CONFIG_PATH,
        "deps": deps_status(),
        "comfy_url": mask_url(COMFY_URL),
        "comfy_url_real_host": clean_base(COMFY_URL),
        "comfy_has_auth": bool(auth_headers(COMFY_URL)),
        "h3_url": mask_url(H3_COMFY_URL or ""),
        "engines": engs,
        "chat_targets": chat_targets_public(),
        "llm_models": (_ceng.scan_models(ldef) if (m and _ceng) else []),
        "llm_active": (_ceng.llm_active_model(ldef) if (m and _ceng) else ""),
        "llm_vram_est": (_ceng.llm_vram_est(ldef) if (m and _ceng) else None),
    }


def save_app_settings(o):
    """保存应用级设置（互斥模式 / 关窗行为）。"""
    global ENGINE_MANAGER
    app = CONFIG.setdefault("app", {})
    if o.get("exclusive") in ("all", "group", "none"):
        app["exclusive"] = o["exclusive"]
        if ENGINE_MANAGER is not None:
            ENGINE_MANAGER.exclusive = o["exclusive"]
    if o.get("close_action") in ("stop_engines", "keep"):
        app["close_action"] = o["close_action"]
    return save_config()


_ALLOWED_TARGET_KEYS = ("name", "group", "kind", "api_url", "api_key", "model", "model_locked")


def save_chat_targets(targets):
    """按目标 id 合并对话目标（只允许改白名单字段），并刷新内存里的 CHAT_TARGETS。"""
    global CHAT_TARGETS
    if not isinstance(targets, dict):
        return False, "targets 必须是对象"
    cur = CONFIG.setdefault("chat_targets", {})
    for tid, patch in targets.items():
        if not isinstance(patch, dict):
            continue
        t = cur.setdefault(tid, {})
        for k in _ALLOWED_TARGET_KEYS:
            if k in patch:
                t[k] = patch[k]
    ok, err = save_config()
    if ok:
        CHAT_TARGETS = CONFIG.get("chat_targets", {}) or {}
    return ok, err


def _upstream_error_text(body_txt):
    """把上游的错误响应提炼成人话，好让前端能直接显示给用户。

    llama.cpp 的格式：
      {"error":{"code":400,"message":"request (93597 tokens) exceeds the available
       context size (65536 tokens), try increasing it","type":"exceed_context_size_error"}}
    有些网关则直接用 {"error":"..."} 或 {"message":"..."}。
    """
    if not body_txt:
        return ""
    try:
        j = json.loads(body_txt)
    except Exception:
        return body_txt.strip()[:400]
    err = j.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)[:400]
    if err:
        return str(err)[:400]
    return str(j.get("message") or body_txt)[:400]


def chat_stream(handler, payload):
    """SSE 流式转发：前端 → 后端 → 上游（OpenAI 兼容），逐块 chunked 写回。

    关键点：
      * 只在本请求内把 protocol_version 提成 HTTP/1.1（不影响其它路由）。
      * 手写 chunked：wfile.write + flush，保证浏览器逐字显示。
      * 前端断开（点"停止"）会触发 BrokenPipeError → 关上游连接 → 上游停止生成。
    """
    up, err = _chat_upstream(payload.get("target") or "local")
    if err:
        handler._send(400, {"ok": False, "error": err})
        return
    t = up["target"]
    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        handler._send(400, {"ok": False, "error": "messages 不能为空"})
        return
    model = payload.get("model") or t.get("model") or ""
    body = {"model": model, "messages": messages, "stream": True}
    for k in ("temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty"):
        if payload.get(k) is not None:
            body[k] = payload[k]
    if payload.get("stop"):
        body["stop"] = payload["stop"]

    hdrs = {"Content-Type": "application/json", "Accept": "text/event-stream",
            "User-Agent": t.get("user_agent") or _BROWSER_UA}
    if t.get("api_key"):
        hdrs["Authorization"] = "Bearer " + t["api_key"]

    # 让上游把自己的性能统计吐出来（前端要在每条回答下方标「响应时间 / 输出量 / 速度」）：
    #   stream_options.include_usage → 末帧带 usage{prompt_tokens, completion_tokens, total_tokens}
    #   timings_per_token            → llama.cpp 专有，给每 token 的 timings 明细
    # 但这些字段不是所有中转网关都认，硬塞可能直接 400 → 先试带、失败再退回纯净请求。
    tid = payload.get("target") or "local"
    extras = {"stream_options": {"include_usage": True}}
    if tid == "local":
        extras["timings_per_token"] = True
    # 思维链开关（前端 #think-toggle，默认开）：
    #   本地 llama.cpp → 请求级 chat_template_kwargs.enable_thinking，开/关都显式下发，
    #     覆盖服务端 CLI 默认，切开关不用重启引擎；
    #   网关类 → OpenAI 风格 enable_thinking，不认时由 400 回退自动丢弃 extras，不会打挂。
    think = bool(payload.get("thinking"))
    if tid == "local":
        extras["chat_template_kwargs"] = {"enable_thinking": think}
    elif think:
        extras["enable_thinking"] = True

    def _open(use_extras):
        b = dict(body)
        if use_extras:
            b.update(extras)
        _req = urllib.request.Request(up["url"], data=json.dumps(b).encode("utf-8"),
                                      headers=hdrs, method="POST")
        return urllib.request.urlopen(_req, timeout=900)

    started = time.time()
    upstream = None
    try:
        upstream = _open(True)
    except urllib.error.HTTPError as e:
        code = int(getattr(e, "code", 0) or 0)
        # ⚠ 先把 body 读出来再 close —— 上游的真实原因全在这里。
        #   llama.cpp 上下文超限时返回 {"error":{"code":400,"message":"request (N tokens)
        #   exceeds the available context size (M tokens)…","type":"exceed_context_size_error"}}，
        #   以前这里把 body 丢掉、只回一个笼统的 502，用户看到"502"根本不知道发生了什么。
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            e.close()
        except Exception:
            pass
        detail = _upstream_error_text(body_txt) or ("上游返回 HTTP %s" % code)
        low = body_txt.lower()
        # 上下文超限 / 参数非法这类是**确定性**错误，原样重试一次也还是错，直接报出来
        fatal = ("exceed" in low) or ("context size" in low)
        if code in (400, 404, 405, 415, 422, 500) and not fatal:
            try:
                upstream = _open(False)
                log_add("chat", "上游不认 usage/timings 参数，已退回普通流式（HTTP %s）" % code, "w")
            except Exception as e2:
                log_add("chat", "上游拒绝请求：%s｜%s" % (e2, detail[:300]), "e")
                handler._send(400, {"ok": False, "error": detail})
                return
        else:
            log_add("chat", "上游拒绝请求：HTTP %s｜%s" % (code, detail[:300]), "e")
            # 用 400 而不是 502：这是"请求本身有问题"，不是网关坏
            handler._send(400, {"ok": False, "error": detail})
            return
    except Exception as e:
        log_add("chat", "连不上上游：%s" % e, "e")
        handler._send(502, {"ok": False, "error": "连不上上游：%s" % e})
        return

    handler.protocol_version = "HTTP/1.1"
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()

    def cw(data):
        handler.wfile.write(("%x\r\n" % len(data)).encode("ascii") + data + b"\r\n")
        handler.wfile.flush()

    log_add("chat", "提问 → %s / %s" % (tid, model or "-"))
    try:
        cw(("event: meta\ndata: " + json.dumps(
            {"target": payload.get("target"), "model": model, "url": up["url"]},
            ensure_ascii=False) + "\n\n").encode("utf-8"))
        for raw in upstream:
            if not raw.strip():
                continue
            cw(raw if raw.endswith(b"\n\n") else raw.rstrip(b"\r\n") + b"\n\n")
        cw(b"data: [DONE]\n\n")
        log_add("chat", "回答完成：%s / %s，用时 %.1fs" % (
            payload.get("target"), model, time.time() - started), "k")
    except BrokenPipeError:
        log_add("chat", "用户点了停止（客户端断开），已关上游连接", "w")
    except (ConnectionAbortedError, ConnectionResetError):
        # ⚠ Windows 上"客户端主动中止"抛的是 ConnectionAbortedError（WinError 10053），
        #   不是 BrokenPipeError（那是 10054 / EPIPE）。不在这里接住的话，
        #   用户点「停止生成」/刷新页面/重新发送，都会被记成一条 [e] 错误，
        #   看着像服务出问题了——其实完全正常。归到同一类并降级为 warn。
        log_add("chat", "客户端中止了连接（停止生成 / 刷新 / 重新发送），已关上游连接", "w")
    except Exception as e:
        try:
            cw(("event: error\ndata: " + json.dumps({"error": str(e)}, ensure_ascii=False) + "\n\n").encode("utf-8"))
        except Exception:
            pass
        log_add("chat", "流式转发出错：%s" % e, "e")
    finally:
        try:
            if upstream is not None:
                upstream.close()
        except Exception:
            pass
        try:
            handler.wfile.write(b"0\r\n\r\n")
            handler.wfile.flush()
        except Exception:
            pass


def main():
    global _SERVER
    mgr = init_engine_manager()
    srv = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    _SERVER = srv
    # 心跳守望：页面（浏览器窗口）关掉后，没有心跳且没有引擎在跑 → 自动退出
    threading.Thread(target=_hb_watchdog, args=(time.time(),), daemon=True).start()
    log_add("app", "Comfy 第三方前端 后端已启动 → http://127.0.0.1:%d（日志落盘 %s）" % (
        LISTEN_PORT, LOG_DIR), "k")
    print("Comfy 第三方前端 已启动 → http://127.0.0.1:%d" % LISTEN_PORT)
    print("  ComfyUI: %s | 工作流: %s" % (COMFY_URL, WORKFLOWS))
    print("  H3 端点: %s (mode=%s)" % (H3.get("endpoint", "(未配置)"), H3.get("mode", "local")))
    if mgr:
        print("  引擎管理: 已就绪（互斥=%s，手动启动）%s" % (mgr.exclusive, list(mgr.engines)))
    else:
        print("  引擎管理: 未启用（comfy_engine 缺失）")
    print("  用户目录: %s" % HOME_DIR)
    print("  心跳保活: %s（%ds 无心跳且无引擎运行 → 自动退出）" % (
        "开" if AUTO_EXIT else "关", int(HB_TIMEOUT)))
    _d = deps_status().get("comfy_cli") or {}
    if _d.get("ok"):
        print("  依赖自检: comfy-cli OK（UI→API 转换可用）")
    else:
        print("  依赖自检: !! comfy-cli 缺失 → 文生图/图生图会失败（%s）" % _d.get("error"))
    print("  Ctrl+C 退出")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    # 退出流程会先调 srv.shutdown()（本函数随即返回）。这时不要把主线程放走：
    # 一旦主线程开始关解释器，而收尾线程还在 print，就会撞上
    # "Fatal Python error: _enter_buffered_busy ... due to daemon threads"，
    # 日志很难看、退出码也可能非 0。让收尾线程做完 os._exit(0) 即可。
    if _QUITTING["v"]:
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
