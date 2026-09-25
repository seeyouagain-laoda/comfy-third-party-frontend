#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ComfyUI 控制脚本 —— 让 AI / 脚本驱动本机 ComfyUI（Comfy Desktop）。

原理：优先「直连 HTTP」——复用 comfy-cli 的 UI→API 转换器把 ComfyUI 的
「UI 导出格式」工作流（含 subgraph 子图）转成 API 格式，然后直接 POST 给本机
`/prompt` 并轮询 `/history`。这样可绕开 comfy-cli 1.20 里过严的 CQL 校验
（它会把「可选 autogrow 输入未连线」误判为致命错误，例如官方 Qwen-Image 2.1
t2i 模板的 `TextEncodeQwenImage21.images`，服务器本身是接受的）。
若直连不可用，自动回退 `comfy run --workflow`。

用法：
  python comfy_control.py status
  python comfy_control.py run "<工作流.json>" --prompt "描述文字" --steps 25 \
        --outdir "X:\\你的出图目录"
  python comfy_control.py run "<工作流.json>" --set "452.prompt=xxx" --set "458.seed=123"
  python comfy_control.py run "<工作流.json>" --engine cli        # 强制走 comfy-cli
  python comfy_control.py print-prompt "<工作流.json>"

要点：
  * 会自动清掉环境里残留(可能已失效)的 HTTP(S)_PROXY，避免 localhost:8188 被代理拦截。
  * 需要 ComfyUI 正在运行（Comfy Desktop 启动后监听 127.0.0.1:8188）。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# --- 1) 关键：清代理，放行 localhost ---
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1,192.168.1.100"
os.environ.setdefault("PYTHONUTF8", "1")
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VENV = os.environ.get("COMFY_VENV", r"C:\你的Python环境")
COMFY = os.environ.get("COMFY_BIN", os.path.join(VENV, "Scripts", "comfy.exe"))
HOST = os.environ.get("COMFY_HOST", "127.0.0.1")
PORT = os.environ.get("COMFY_PORT", "8188")
BASE_URL = "http://%s:%s" % (HOST, PORT)
OUTPUT_DIR = os.environ.get("COMFY_OUTPUT", r"X:\你的ComfyUI输出目录")
# 支持多目录（用 ; 或 , 分隔）——Comfy Desktop 与「无头启动」的默认输出目录常常不同，
# 只认一个目录会导致「明明生成成功、画廊却是空的」。
OUTPUT_DIRS = [d.strip() for d in OUTPUT_DIR.replace(",", ";").split(";") if d.strip()]
OUTPUT_DIR = OUTPUT_DIRS[0] if OUTPUT_DIRS else OUTPUT_DIR
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")

# --- 2) 注入 venv 的 site-packages，复用 comfy-cli 的 UI→API 转换器 ---
VENV_SP = os.path.join(VENV, "Lib", "site-packages")
if os.path.isdir(VENV_SP) and VENV_SP not in sys.path:
    sys.path.insert(0, VENV_SP)


def walk_nodes(obj):
    """yield 所有 node dict（含顶层 nodes 与 definitions.subgraphs 内的 nodes）。"""
    if isinstance(obj, dict):
        nodes = obj.get("nodes")
        if isinstance(nodes, list):
            for n in nodes:
                if isinstance(n, dict):
                    yield n
        for v in obj.values():
            yield from walk_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk_nodes(v)


def node_has_widget(node, name):
    wvn = node.get("widgets_values_named")
    if isinstance(wvn, dict) and name in wvn:
        return True
    inp = node.get("inputs")
    if isinstance(inp, dict) and name in inp:
        return True
    if isinstance(inp, list):
        for it in inp:
            if isinstance(it, dict) and it.get("name") == name:
                return True
    return False


def set_widget(node, name, value):
    wvn = node.get("widgets_values_named")
    wv = node.get("widgets_values")
    if isinstance(wvn, dict) and name in wvn:
        keys = list(wvn.keys())
        i = keys.index(name)
        wvn[name] = value
        if isinstance(wv, list) and i < len(wv):
            wv[i] = value
        return True
    inp = node.get("inputs")
    if isinstance(inp, dict) and name in inp:
        inp[name] = value
        return True
    return False


def find_widget_nodes(wf, name):
    ids = []
    for n in walk_nodes(wf):
        if node_has_widget(n, name):
            ids.append(n.get("id"))
    return ids


def coerce(raw):
    try:
        return json.loads(raw)
    except Exception:
        return raw


def build_patches(wf, a):
    """把 --prompt/--negative/--seed/--steps/--set 汇总成 [(node_id, widget, value)]。"""
    patches = []

    def auto_widget(widget_name, value):
        ids = find_widget_nodes(wf, widget_name)
        if not ids:
            print("  !! 找不到含 widget '%s' 的节点，忽略" % widget_name)
            return
        patches.append((ids[0], widget_name, value))

    if a.prompt is not None:
        auto_widget("prompt", a.prompt)
    if a.negative is not None:
        auto_widget("negative_prompt", a.negative)
    if a.seed is not None:
        auto_widget("seed", a.seed)
    if a.steps is not None:
        auto_widget("steps", a.steps)
    if a.cfg is not None:
        auto_widget("cfg", a.cfg)
    for spec in (a.set or []):
        head = spec.split("=", 1)[0]
        if ":" in head and ("." not in head or head.index(":") < head.index(".")):
            nid, _, rest = spec.partition(":")      # NODE:widget=VALUE（兼容写法）
        else:
            nid, _, rest = spec.partition(".")      # NODE.widget=VALUE（标准）
        name, _, raw = rest.partition("=")
        if not nid or not name:
            print("  !! --set 格式应为 NODE_ID.widget=VALUE，忽略: %s" % spec)
            continue
        patches.append((nid, name, coerce(raw)))
    return patches


def apply_patches(wf, patches):
    log = []
    for nid, name, value in patches:
        done = False
        for n in walk_nodes(wf):
            if str(n.get("id")) == str(nid):
                if set_widget(n, name, value):
                    log.append("  %s.%s = %s" % (nid, name, json.dumps(value, ensure_ascii=False)))
                    done = True
                    break
        if not done:
            log.append("  !! MISS %s.%s（该节点无此 widget）" % (nid, name))
    return log


def call_comfy(args, timeout=None, with_target=False):
    """构造并执行 comfy-cli 命令。

    注意：--host/--port 不是全局参数，只属于 `run` 等子命令（comfy-cli 1.20 实测），
    因此仅在 with_target=True 时追加，且必须排在子命令之后。
    """
    cmd = [COMFY, "--json", "--where", "local", "--skip-prompt"] + args
    if with_target:
        cmd += ["--host", HOST, "--port", PORT]
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def parse_envelope(stdout):
    for line in reversed([l for l in stdout.splitlines() if l.strip()]):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("schema"):
            return d
    return None


def snapshot_outputs():
    seen = {}
    for od in OUTPUT_DIRS:
        if not os.path.isdir(od):
            continue
        for root, _, files in os.walk(od):
            for f in files:
                p = os.path.join(root, f)
                try:
                    seen[p] = os.path.getmtime(p)
                except OSError:
                    pass
    return seen


def collect_new_outputs(before, since):
    """挑出本次任务真正新产出/被改写的图片。

    ⚠ 曾经的写法是 `p not in before or m > since - 1`，那个 **-1 秒宽容窗口**是个坑：
    连续两次任务（如「文生图 → 紧接着图生图」）时，上一次的产物 mtime 落在
    [since-1, since] 区间里，会被当成新产物一起返回（实测图生图结果里混进了
    上一步的输入图）。现在改为**只跟快照比对**：快照里没有的（新文件），
    或快照后 mtime 变了的（被改写），才算新产物。精确到 NTFS 的 100ns，无时间窗误差。
    """
    news = []
    for od in OUTPUT_DIRS:
        if not os.path.isdir(od):
            continue
        for root, _, files in os.walk(od):
            for f in files:
                if not f.lower().endswith(IMG_EXT):
                    continue
                p = os.path.join(root, f)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if p not in before or before.get(p) != m:
                    news.append(p)
    return sorted(news, key=lambda p: os.path.getmtime(p))


def http_json(url, payload=None, timeout=120):
    """极简 HTTP JSON 调用（stdlib，不依赖 requests）。"""
    import urllib.request
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw) if raw.strip() else {}


def get_object_info():
    return http_json(BASE_URL + "/object_info", timeout=300)


# 纯展示节点：无输出、不参与出图，但 ComfyUI 核心升级后可能新增「必填」输入
# （如 ImageCompare 旧工作流缺 compare_view），导致 /prompt 校验失败。
# 提交前直接剔除，不影响生成结果。
_DISPLAY_ONLY_NODES = {"ImageCompare"}


def _strip_display_nodes(api_prompt):
    """剔除纯展示节点（无输出、仅 UI 预览用），避免 /prompt 校验因缺必填输入而失败。"""
    if not isinstance(api_prompt, dict):
        return api_prompt
    drop = [nid for nid, node in api_prompt.items()
            if isinstance(node, dict) and node.get("class_type") in _DISPLAY_ONLY_NODES]
    for nid in drop:
        api_prompt.pop(nid, None)
    if drop:
        print("  已剔除纯展示节点：%s（不影响出图）"
              % ", ".join(sorted(str(x) for x in drop)))
    return api_prompt


def convert_ui_to_api(wf):
    """用 comfy-cli 自带的转换器把 UI 工作流转成 API 格式（支持 subgraph）。

    说明：只借它的转换能力，不用它的 `run` 校验——comfy-cli 1.20 的 CQL 校验
    会把「可选 autogrow 输入未连线」误判为致命错误（例如官方 Qwen-Image 2.1
    t2i 模板的 TextEncodeQwenImage21.images），而 ComfyUI 服务器本身是接受的。
    """
    from comfy_cli.workflow_to_api import convert_ui_to_api as _c, is_api_format
    if is_api_format(wf):
        return wf
    return _strip_display_nodes(_c(wf, get_object_info()))


def _history_error_text(entry):
    st = entry.get("status") or {}
    msgs = st.get("messages") or []
    lines = []
    for m in msgs:
        try:
            kind, payload = m[0], m[1]
        except Exception:
            continue
        if kind in ("execution_error", "execution_interrupted"):
            lines.append(str(payload))
    return "\n".join(lines) if lines else json.dumps(st, ensure_ascii=False)[:2000]


def direct_submit(api_prompt, timeout_s, poll=2.0, cancel_check=None, on_pid=None,
                  client_id=None):
    """直接 POST /prompt 并轮询 /history 等待完成（绕过 comfy-cli 的过严校验）。
    cancel_check: 每轮轮询前调用，返回 True 则中止并返回 {"cancelled": True}。
    on_pid: 拿到 prompt_id 后回调（供上层做 /interrupt）。
    client_id: 指定 ComfyUI 的 client_id。必须与监听 /ws 的那个 id 一致，
               否则 ComfyUI 的 progress/executing 事件（定向发给提交者）收不到。"""
    import uuid
    cid = client_id or str(uuid.uuid4())
    try:
        res = http_json(BASE_URL + "/prompt",
                        {"prompt": api_prompt, "client_id": cid},
                        timeout=120)
    except Exception as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")   # HTTPError 才有
        except Exception:
            pass
        try:
            return False, json.loads(body)
        except Exception:
            return False, {"error": repr(e), "body": body[:3000]}
    pid = res.get("prompt_id")
    if not pid:
        return False, res
    print("  prompt_id = %s" % pid)
    if on_pid:
        try:
            on_pid(pid)
        except Exception:
            pass
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        if cancel_check:
            try:
                if cancel_check():
                    return False, {"cancelled": True, "error": "已暂停生成", "prompt_id": pid}
            except Exception:
                pass
        try:
            h = http_json("%s/history/%s" % (BASE_URL, pid), timeout=60)
        except Exception:
            h = {}
        entry = h.get(pid) if isinstance(h, dict) else None
        if isinstance(entry, dict):
            st = entry.get("status") or {}
            last = st.get("status_str")
            if st.get("completed") is True or st.get("status_str") in ("success", "error"):
                if st.get("status_str") == "error":
                    return False, {"prompt_id": pid, "status": st,
                                   "error_text": _history_error_text(entry)}
                return True, entry
        time.sleep(poll)
    return False, {"error": "timeout after %ss" % timeout_s, "prompt_id": pid, "last_status": last}


def cmd_status(_):
    import urllib.request
    try:
        r = urllib.request.urlopen(BASE_URL + "/system_stats", timeout=6)
        d = json.load(r)
        print("ComfyUI 在线 ✅  %s" % BASE_URL)
        for dev in d.get("devices", []):
            tot = (dev.get("vram_total") or 0) / 1e9
            fre = (dev.get("vram_free") or 0) / 1e9
            print("  GPU: %s | VRAM %.1f/%.1f GB" % (dev.get("name"), fre, tot))
        return 0
    except Exception as e:
        print("ComfyUI 未运行 ❌  (%s)" % e)
        print("请先启动 Comfy Desktop（监听 %s）再试。" % BASE_URL)
        return 2


def cmd_models(_):
    root = r"X:\你的ComfyUI共享目录\models"
    if os.path.isdir(root):
        for folder in sorted(os.listdir(root)):
            fp = os.path.join(root, folder)
            if not os.path.isdir(fp):
                continue
            items = [f for f in os.listdir(fp)
                     if f.lower().endswith((".gguf", ".safetensors", ".ckpt", ".pt", ".pth", ".bin"))]
            if items:
                print("[%s]" % folder)
                for f in items:
                    sz = os.path.getsize(os.path.join(fp, f)) / 1e9
                    print("   %-70s %.2f GB" % (f, sz))
    root2 = r"X:\你的模型目录"
    if os.path.isdir(root2):
        items = [f for f in os.listdir(root2)
                 if f.lower().endswith((".gguf", ".safetensors"))]
        if items:
            print("[X:\\你的模型目录 (源)]")
            for f in items:
                print("   %-70s %.2f GB" % (f, os.path.getsize(os.path.join(root2, f)) / 1e9))
    return 0


def cmd_upload(a):
    """把本地图片上传到 ComfyUI 的 input 目录（图生图 / 改图 / LoadImage 必需）。"""
    import urllib.request
    import uuid
    if not os.path.isfile(a.file):
        print("文件不存在：%s" % a.file)
        return 1
    name = a.name or os.path.basename(a.file)
    subfolder = a.subfolder or ""
    boundary = "----comfyctl" + uuid.uuid4().hex
    with open(a.file, "rb") as f:
        content = f.read()
    CRLF = b"\r\n"
    fields = []
    for k, v in (("overwrite", "true"), ("subfolder", subfolder)):
        fields.append(("--%s" % boundary).encode("utf-8") + CRLF
                      + ('Content-Disposition: form-data; name="%s"' % k).encode("utf-8")
                      + CRLF + CRLF + str(v).encode("utf-8") + CRLF)
    fields.append(("--%s" % boundary).encode("utf-8") + CRLF
                  + ('Content-Disposition: form-data; name="image"; filename="%s"' % name).encode("utf-8")
                  + CRLF + b"Content-Type: application/octet-stream" + CRLF + CRLF
                  + content + CRLF)
    fields.append(("--%s--" % boundary).encode("utf-8") + CRLF)
    body = b"".join(fields)
    req = urllib.request.Request(
        BASE_URL + "/upload/image", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            res = json.load(r)
    except Exception as e:
        print("上传失败 ❌ %r" % (e,))
        return 1
    out = res.get("name", name)
    if res.get("subfolder"):
        out = res["subfolder"] + "/" + out
    print("上传成功 ✅  在 LoadImage 节点里填：%s" % out)
    print(json.dumps(res, ensure_ascii=False))
    return 0


def cmd_print_prompt(a):
    try:
        with open(a.workflow, "r", encoding="utf-8") as f:
            wf = json.load(f)
        api = convert_ui_to_api(wf)
        print(json.dumps(api, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print("直连转换失败（%r），回退 comfy run --print-prompt…" % (e,), file=sys.stderr)
    p = call_comfy(["run", "--workflow", a.workflow, "--print-prompt"],
                   timeout=180, with_target=True)
    env = parse_envelope(p.stdout)
    if env is not None:
        print(json.dumps(env, ensure_ascii=False, indent=2))
    else:
        print(p.stdout)
        if p.stderr:
            print("--- stderr ---\n" + p.stderr, file=sys.stderr)
    return 0 if p.returncode == 0 else 1


def cmd_run(a):
    # 命令行入口：读工作流 → 按 --set 覆盖参数(build/apply_patches) →
    # 转 API 格式(convert_ui_to_api) → 直连提交(direct_submit)，失败回退 comfy run。
    with open(a.workflow, "r", encoding="utf-8") as f:
        wf = json.load(f)
    patches = build_patches(wf, a)
    log = apply_patches(wf, patches)
    print("== 参数覆盖 ==")
    print("\n".join(log) if log else "  (无)")

    tmpdir = tempfile.mkdtemp(prefix="comfy_wf_")
    tmpwf = os.path.join(tmpdir, os.path.basename(a.workflow))
    with open(tmpwf, "w", encoding="utf-8") as f:
        json.dump(wf, f, ensure_ascii=False, indent=2)

    before = snapshot_outputs()
    start = time.time()
    timeout = a.timeout or 0
    if timeout <= 0:
        timeout = 3600

    engine = a.engine
    api_prompt = None
    if engine in ("auto", "direct"):
        try:
            api_prompt = convert_ui_to_api(wf)
            engine = "direct"
        except Exception as e:
            if engine == "direct":
                print("直连模式失败 ❌ %r" % (e,))
                return 1
            print("  直连模式不可用（%r），回退 comfy-cli…" % (e,))
            engine = "cli"

    if engine == "direct":
        with open(os.path.join(tmpdir, "api_prompt.json"), "w", encoding="utf-8") as f:
            json.dump(api_prompt, f, ensure_ascii=False, indent=2)
        print("== 提交执行（直连 /prompt，已跳过 comfy-cli 校验）==")
        ok, info = direct_submit(api_prompt, timeout)
        if not ok:
            print("执行失败 ❌")
            print(json.dumps(info, ensure_ascii=False, indent=2)[:4000])
            return 1
    else:
        print("== 提交执行（comfy run --wait）==")
        p = call_comfy(["run", "--workflow", tmpwf, "--wait", "--json", "--no-watch"],
                       timeout=timeout, with_target=True)
        env = parse_envelope(p.stdout)
        if env is None:
            print("无法解析 comfy 输出，原始 stdout：")
            print(p.stdout)
            if p.stderr:
                print("--- stderr ---\n" + p.stderr, file=sys.stderr)
            return 1
        if not env.get("ok", False):
            print("执行失败 ❌")
            print(json.dumps(env, ensure_ascii=False, indent=2))
            return 1

    outs = collect_new_outputs(before, start)
    print("执行成功 ✅  用时 %.1fs" % (time.time() - start))
    if not outs:
        print("未在输出目录发现新图片：%s" % OUTPUT_DIR)
        print("  提示：ComfyUI 会按输入哈希缓存——参数完全相同的重复提交会秒返回且不重新落盘。")
        print("        换 --seed 或改任一参数即可强制重新执行。")
        return 0

    final = []
    if a.outdir:
        os.makedirs(a.outdir, exist_ok=True)
        for pth in outs:
            dst = os.path.join(a.outdir, os.path.basename(pth))
            try:
                shutil.copy2(pth, dst)
                final.append(dst)
            except Exception as e:
                print("复制失败 %s -> %s (%s)，保留原路径" % (pth, dst, e))
                final.append(pth)
    else:
        final = outs
    print("出图 %d 张：" % len(final))
    for pth in final:
        print("  " + pth)
    return 0


def main():
    ap = argparse.ArgumentParser(description="ComfyUI 控制脚本（comfy-cli 驱动）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="检查 ComfyUI 是否在运行").set_defaults(func=cmd_status)
    sub.add_parser("models", help="列出本机可用模型").set_defaults(func=cmd_models)

    up = sub.add_parser("upload", help="上传本地图片到 ComfyUI input 目录（LoadImage 用）")
    up.add_argument("file")
    up.add_argument("--name", help="上传后的文件名（默认为原文件名）")
    up.add_argument("--subfolder", help="上传到 input 的子目录")
    up.set_defaults(func=cmd_upload)

    pp = sub.add_parser("print-prompt", help="把 UI 工作流转成 API 格式并打印")
    pp.add_argument("workflow")
    pp.set_defaults(func=cmd_print_prompt)

    rn = sub.add_parser("run", help="提交并等待一个工作流出图")
    rn.add_argument("workflow")
    rn.add_argument("--prompt", help="正面提示词（自动写入含 'prompt' 的节点）")
    rn.add_argument("--negative", help="负面提示词（写入 negative_prompt 节点）")
    rn.add_argument("--seed", type=int)
    rn.add_argument("--steps", type=int)
    rn.add_argument("--cfg", type=float)
    rn.add_argument("--set", action="append", metavar="NODE.widget=VALUE",
                    help="精确覆盖某节点 widget，可重复")
    rn.add_argument("--outdir", help="把输出图片另存到此目录")
    rn.add_argument("--engine", choices=["auto", "direct", "cli"], default="auto",
                    help="提交方式：direct=直接 POST /prompt（默认，绕过 comfy-cli 过严校验）；"
                         "cli=用 comfy run；auto=优先 direct")
    rn.add_argument("--timeout", type=int, default=1800, help="超时秒数，默认 1800")
    rn.set_defaults(func=cmd_run)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
