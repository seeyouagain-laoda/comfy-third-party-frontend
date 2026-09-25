# -*- coding: utf-8 -*-
"""
comfy_studio_launch.py —— Comfy 第三方前端 浏览器版启动器

做什么（用户要求："直接打开我的 Chrome 浏览器，不要单独的网页"）：
  1. 若 8777 已在监听 → 说明后端已在跑，直接开浏览器连过去（单实例）。
  2. 否则以 python 起后端（无黑框）→ 等 /api/init 就绪。
  3. 自动打开浏览器 —— 三种模式，见 config `app.browser_mode`：
     * **daily（默认）**：直接用**用户日常那个 Chrome**（默认 profile）开一个普通标签页。
       书签栏 / 扩展 / 已登录账号全在，用户上手就能用。
       ⚠ 这一路**没有 CDP** —— Chrome 136 起 `--remote-debugging-port` 对默认
       user-data-dir 一律不生效（官方公告 2025-03-17），硬开还会把全部登录会话
       暴露给本机任意程序。AI 要接管页面请走 **browser-skill**
       （`bsk browsers` → `bsk session start` → `bsk tab borrow <tab-id>`，
       靠扩展通信，不需要 CDP）。
     * **isolated**：独立 user-data-dir 的 App 模式窗口 + CDP 调试端口
       （旧行为，保留给需要 Playwright/Puppeteer 直连的场景）；
       端口写进 `data/browser_cdp.json`，页面上 `GET /api/app/cdp` 也能拿到。
     * **system**：交给系统默认浏览器开标签页。
  4. 页面生命周期 = **页面注册表**（不是单一"关闭信标"）：
       前端 加载时登记  GET  /api/app/pageopen?pid=<页面id>
            每 5s 续期 GET  /api/heartbeat?pid=<页面id>
            卸载时注销 POST /api/app/pageclose?pid=<页面id>（sendBeacon）
     后端按 page_id 逐个记账，**一个页面都不剩且持续 page_empty_hold 秒**才停引擎退出；
     另以「心跳超时」（默认 120s，兼容浏览器对后台标签页的定时器节流）兜底。
     ⚠ 多标签页安全的关键：关掉任意一个只注销它自己，绝不当作全局死讯
       —— 用日常 Chrome 后必然同时开着好几个 Comfy 第三方前端 标签页。

设计原则：**零侵入**。ComfyUI / llama.cpp 的内部文件一个字节都不动，
只以子进程 + 命令行参数拉起，之后全部走它们自带的 HTTP 接口。
打开用户日常 Chrome 时也不加 `--user-data-dir` / `--no-proxy-server` /
`--disable-features` —— 不去改用户浏览器的任何全局行为。
（2026-09-23 起 H3 视频也并入 ComfyUI，不再需要旧整合包自带的 API 服务。）

用法：
  pythonw comfy_studio_launch.py                # 正常启动（按 browser_mode 开浏览器）
  python  comfy_studio_launch.py --tab          # 强制用系统默认浏览器标签页
  python  comfy_studio_launch.py --no-browser   # 只起后端（调试用）
  python  comfy_studio_launch.py --stop         # 停掉正在跑的后端并退出
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP_NAME = "Comfy 第三方前端"
HERE = os.path.dirname(os.path.abspath(__file__))

# ⚠️ 本机 HKLM 里常挂着 HTTP_PROXY=http://127.0.0.1:7897（Clash/Mihomo），
#    urllib 会把「访问 127.0.0.1」也丢给代理，代理对 localhost 常常直接 502。
#    启动器全程只连本机 → 清掉代理变量，并把 NO_PROXY 设好传给子进程 / 浏览器。
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"

HOME = os.environ.get("COMFYSTUDIO_HOME") or HERE
LOG_DIR = os.path.join(HOME, "logs")
DATA_DIR = os.path.join(HOME, "data")
for _d in (LOG_DIR, DATA_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass
LOG_FILE = os.path.join(LOG_DIR, "launcher.log")

CONFIG_PATH = os.path.join(HOME, "comfy_studio_config.json")
if not os.path.isfile(CONFIG_PATH):
    CONFIG_PATH = os.path.join(HERE, "comfy_studio_config.json")

# 只连 127.0.0.1 的裸 opener（不读 HTTP_PROXY / 注册表代理设置）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(*a):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        if sys.stdout is not None:
            print(line, flush=True)
    except Exception:
        pass


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        log("配置读取失败，用默认值：", e)
        return {}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def port_open(port, host="127.0.0.1", timeout=0.6):
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def http_json(path, timeout=6.0, method="GET"):
    # ⚠️ 必须绕开系统代理：本机（HKLM 环境变量）常挂着 HTTP_PROXY=http://127.0.0.1:7897
    #    （Clash/Mihomo）。urllib 会老老实实把 http://127.0.0.1:8777 的请求丢给代理，
    #    代理对 localhost 常常直接回 502 → 启动器就会误判"后端没起来"。
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    req = urllib.request.Request(url, data=(b"{}" if method == "POST" else None),
                                method=method,
                                headers={"Content-Type": "application/json"})
    with _NO_PROXY_OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def wait_backend(seconds=90):
    """等 /api/init 真的能应答（不是只看端口）。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            http_json("/api/init", timeout=4)
            return True
        except Exception:
            time.sleep(0.5)
    return False


def find_browser(prefer="chrome", explicit=""):
    """返回 (可执行文件路径, 名字)；找不到返回 (None, None)。

    prefer:   "chrome"（默认——要开 CDP 调试端口）/ "edge"
    explicit: config 里 app.browser_exe 指定的全路径，优先用
    """
    if explicit and os.path.isfile(explicit):
        return explicit, os.path.basename(explicit)[:-4].capitalize()
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    chrome = [
        (os.path.join(pf, r"Google\Chrome\Application\chrome.exe"), "Chrome"),
        (os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"), "Chrome"),
        (os.path.join(lad, r"Google\Chrome\Application\chrome.exe"), "Chrome"),
    ]
    edge = [
        (os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"), "Edge"),
        (os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"), "Edge"),
        (os.path.join(lad, r"Microsoft\Edge\Application\msedge.exe"), "Edge"),
    ]
    order = (edge + chrome) if str(prefer).lower() == "edge" else (chrome + edge)
    for p, n in order:
        if p and os.path.isfile(p):
            return p, n
    return None, None


# --------------------------------------------------------------------------- #
# CDP（Chrome DevTools Protocol）——"带接口的浏览器"
# --------------------------------------------------------------------------- #
CDP_CANDIDATES = (9411, 9412, 9413, 9414, 9415)


def cdp_json_path():
    return os.path.join(DATA_DIR, "browser_cdp.json")


def read_cdp_json():
    try:
        with open(cdp_json_path(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def cdp_alive(port, timeout=1.5):
    """该端口上是否真有活的 Chrome DevTools（/json/version 有应答）。"""
    try:
        port = int(port or 0)
    except Exception:
        return False
    if not port:
        return False
    try:
        with _NO_PROXY_OPENER.open("http://127.0.0.1:%d/json/version" % port, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def read_devtools_active_port(udd):
    """Chrome 启动后会把**实际**调试端口写进 <user-data-dir>/DevToolsActivePort 第一行。
    用 `--remote-debugging-port=0` 时由 Chrome 自己挑端口，靠这个文件回读。"""
    try:
        with open(os.path.join(udd, "DevToolsActivePort"), "r", encoding="utf-8") as f:
            v = (f.readline() or "").strip()
        return int(v) if v.isdigit() else 0
    except Exception:
        return 0


def pick_free_port(cands):
    for p in cands or CDP_CANDIDATES:
        if not port_open(p):
            return int(p)
    return 0


def wait_cdp(port, udd, timeout=25.0):
    """等 CDP 起来并回读真实端口；失败返回 0。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = read_devtools_active_port(udd)
        if p and cdp_alive(p, timeout=1.0):
            return p
        if port and cdp_alive(port, timeout=1.0):
            return int(port)
        time.sleep(0.4)
    return 0


def write_cdp_json(port, page_url, exe, name, udd, mode="isolated"):
    """把调试端口落盘，供外部 AI / 自动化工具发现并接管这个页面。

    mode:  "isolated" = 独立 profile + CDP，外部工具可直接连；
           "daily" / "system" = 用的是用户日常浏览器，**没有 CDP**
           （Chrome 136+ 起 --remote-debugging-port 对默认 user-data-dir 一律不生效），
           这里显式写 cdp=false + alt 指引，免得外部工具拿着 port=0 瞎连。
    """
    info = {
        "mode": mode,
        "cdp": bool(port),
        "port": int(port or 0),
        "http": ("http://127.0.0.1:%d" % port) if port else "",
        "version_url": ("http://127.0.0.1:%d/json/version" % port) if port else "",
        "targets_url": ("http://127.0.0.1:%d/json" % port) if port else "",
        "page_url": page_url,
        "browser": name,
        "exe": exe,
        "user_data_dir": udd,
        "alt": "" if port else "无 CDP。要操控这个页面请用 browser-skill："
                               "bsk browsers → bsk session start → bsk tab borrow <tab-id>",
        "ts_h": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(cdp_json_path(), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("写 browser_cdp.json 失败：", e)
    return info


def _geo_args(geo):
    out = []
    if not geo:
        return out
    out.append("--window-size=%d,%d" % (int(geo.get("width") or 1500), int(geo.get("height") or 950)))
    if geo.get("x") is not None and geo.get("y") is not None:
        out.append("--window-position=%d,%d" % (int(geo["x"]), int(geo["y"])))
    return out


def open_browser(url, app_mode=True, geo=None, acfg=None):
    """开窗。返回 (进程对象 或 None, 说明文字)。

    三种模式（config `app.browser_mode`）：

      * **"daily"（默认）** —— 直接用**用户日常那个 Chrome**（默认 profile）开一个普通标签页。
        书签栏 / 扩展 / 已登录的账号全都在，用户上手就能用，不再被关在隔离的空壳 profile 里。
        代价是**没有 CDP 调试端口**：Chrome 136 起官方明确 `--remote-debugging-port`
        对默认 user-data-dir 一律不生效（Chrome for Developers, 2025-03-17），
        且硬开等于把用户全部登录会话暴露给本机任意程序。AI 要接管页面请走
        **browser-skill**（bsk CLI + 浏览器扩展，不需要 CDP）。

      * **"isolated"** —— 独立 user-data-dir + `--app=` + CDP 调试端口（旧行为）。
        外部工具能直接 CDP 接管；代价是这个 profile 里没有书签 / 登录态，
        而且会被机器上已在跑的 Chrome 隔离成一个"单独的窗口"。

      * **"system"** —— 交给系统默认浏览器开标签页（连 chrome.exe 都不指定）。

    为什么 isolated 必须用我们自己的 --user-data-dir：
      * 独立用户目录 → 不会被机器上已在跑的 Chrome 收编（否则新进程秒退、调试端口不生效）；
      * 独立用户目录 → 这个进程是我们亲生的，能 Popen().poll() 等它退出来判断「关窗」。
    """
    acfg = acfg or {}
    bmode = (acfg.get("browser_mode") or "daily").strip().lower()
    mode = (acfg.get("window_mode") or "app").lower()
    if not app_mode:
        mode = "tab"

    # ---------- 分支 A：daily / system —— 用户日常浏览器，普通标签页，无 CDP ----------
    if bmode in ("daily", "system"):
        exe, name = find_browser(acfg.get("browser_prefer") or "chrome",
                                 acfg.get("browser_exe") or "")
        if bmode == "system" or not exe:
            import webbrowser
            webbrowser.open(url)
            write_cdp_json(0, url, exe or "", name or "默认浏览器", "", mode=bmode)
            return None, "系统默认浏览器（标签页，无 CDP）"

        # ⚠ 三条「不要」，都是为了不动用户的浏览器：
        #   不加 --user-data-dir     → 用他的默认 profile，书签/扩展/登录态全在
        #   不加 --remote-debugging-port → Chrome 136+ 对默认 profile 直接忽略，加了白加
        #   不加 --no-proxy-server / --disable-features → 别去改他浏览器的全局行为
        args = ["--new-window", url]
        try:
            subprocess.Popen([exe] + args, creationflags=0x00000008, close_fds=True)
        except Exception as e:
            log("启动 %s 失败：%s → 退回默认浏览器" % (name, e))
            import webbrowser
            webbrowser.open(url)
            return None, "默认浏览器（%s 启动失败）" % name

        write_cdp_json(0, url, exe, name, "", mode=bmode)
        log("已在日常 %s 里开新窗口打开 %s（默认 profile、无 CDP）" % (name, url))
        # 返回 None = 「普通标签页」语义：启动器不去 poll 进程，
        # 关页面由前端 pagehide 信标 + 心跳兜底通知后端收尾（多标签页也安全）。
        return None, "%s 日常浏览器新窗口（书签/扩展/登录态都在；无 CDP）" % name

    # ---------- 分支 B：系统默认浏览器（`--tab` 或 window_mode=tab） ----------
    if mode == "tab":
        import webbrowser
        webbrowser.open(url)
        write_cdp_json(0, url, "", "默认浏览器", "", mode="system")
        return None, "系统默认浏览器（标签页，无 CDP）"

    # ---------- 分支 C：isolated —— 独立 profile 窗口 + CDP ----------
    exe, name = find_browser(acfg.get("browser_prefer") or "chrome", acfg.get("browser_exe") or "")
    if not exe:
        import webbrowser
        webbrowser.open(url)
        write_cdp_json(0, url, "", "默认浏览器", "", mode="system")
        return None, "未找到 Chrome/Edge → 系统默认浏览器（标签页）"

    # ⚠️ 用户目录按浏览器分开：Edge 与 Chrome 的 profile 目录互不兼容，
    #    混用会报 "profile not compatible" 甚至直接退。
    udd = acfg.get("user_data_dir") or os.path.join(DATA_DIR, "browser-" + name.lower())
    base = [
        "--user-data-dir=" + udd,
        "--no-first-run", "--no-default-browser-check",
        "--no-proxy-server",                       # 页面零外链；顺手排掉系统代理误拦 localhost
        # 防后台节流：窗口被遮挡时 setTimeout 会被钳到 ~1s，CDP 自动化会大面积超时
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=Translate,msEdgeIdentityFeatures",
    ]
    head = ["--app=" + url] if mode == "app" else [url]

    # --- 情形 1：上一次的 Studio 窗口还在跑（同 user-data-dir）→ 复用它的 CDP，只开新窗口 ---
    prev = read_cdp_json()
    if int(prev.get("port") or 0) and prev.get("user_data_dir") == udd and cdp_alive(prev.get("port")):
        log("上次的 Studio 窗口仍在运行 → 复用已有实例的 CDP %s，只开新窗口" % prev.get("port"))
        try:
            p = subprocess.Popen([exe] + head + base + _geo_args(geo),
                                 creationflags=0x00000008, close_fds=True)
            return p, "复用已有 %s 实例（CDP %s 已在）" % (name, prev.get("port"))
        except Exception as e:
            log("复用已有实例失败：%s → 改为全新启动" % e)

    # --- 情形 2：全新启动 ---
    #   remote_debug_port: -1 = 关掉调试端口；0 = 交给 Chrome 自动分配（推荐）；>0 = 指定端口
    try:
        want = int(acfg.get("remote_debug_port") or 0)
    except Exception:
        want = 0
    if want == -1:
        want = -2                                    # 哨兵：关闭
    port = 0
    if want != -2 and want > 0:
        port = int(want) if not port_open(want) else pick_free_port(acfg.get("cdp_port_candidates"))
        if not port:
            log("!! 端口 %d 与候选端口全被占用 → 本次不开调试端口" % want)
            want = -2

    args = head + base
    if want != -2:
        # 清掉上次残留的端口记录，避免 wait_cdp 读到过期端口
        try:
            os.remove(os.path.join(udd, "DevToolsActivePort"))
        except Exception:
            pass
        args.append("--remote-debugging-port=%d" % port)   # 0 → Chrome 自己挑
        args.append("--remote-allow-origins=*")            # Chrome 111+ 必需，否则 ws 被拒
    args += _geo_args(geo)

    try:
        p = subprocess.Popen([exe] + args, creationflags=0x00000008, close_fds=True)
    except Exception as e:
        log("启动 %s 失败：%s → 退回默认浏览器" % (name, e))
        import webbrowser
        webbrowser.open(url)
        return None, "默认浏览器（%s 启动失败）" % name

    if want == -2:
        log("已打开 %s 窗口 pid=%s（调试端口已关闭）" % (name, p.pid))
        return p, "%s 独立窗口（无 CDP）" % name

    got = wait_cdp(port, udd, timeout=25)
    if got:
        info = write_cdp_json(got, url, exe, name, udd)
        log("已打开 %s pid=%s，CDP 就绪：%s" % (name, p.pid, info["version_url"]))
        return p, "%s %s窗口（CDP %d）" % (name, "App " if mode == "app" else "", got)

    log("!! %s 已启动但 CDP 未就绪（预期端口 %s）——外部工具暂时接管不了" % (name, port or "自动"))
    write_cdp_json(0, url, exe, name, udd)
    return p, "%s 窗口（CDP 未就绪）" % name


def page_alive(probe=14.0, dead_age=12.0, poll=1.5, left_window=90.0):
    """还有页面在看吗？——判断 App 窗口是真关了，还是浏览器进程交接导致的误判。

    判据是后端的**页面注册表**（`/api/heartbeat?peek=1` 返回的 `pages` 字段）：
    前端每个标签页加载时 pageopen 登记、心跳带 pid 续期、卸载时只注销自己。于是

        pages > 0   → 还有页面在看（哪怕它被浏览器后台节流、几十秒才打一次心跳）
        pages == 0  → 一个页面都不剩了

    比旧的「关闭信标 + age」两段式可靠：多标签页时关掉其中一个标签页，
    不会影响其它标签页的判定（旧写法会把还开着的页面一起判死）。

    探针必须带 ?peek=1（只读）——否则自己把心跳时间刷新掉，永远判「活着」。
    （dead_age / left_window 保留在签名里只为兼容既有调用，不再使用。）
    """
    t0 = time.time()
    while True:
        try:
            st = http_json("/api/heartbeat?peek=1", timeout=4)
        except Exception:
            return False
        try:
            pages = int(st.get("pages") or 0)
        except Exception:
            pages = 0
        if pages > 0:
            log("页面注册表里还有 %d 个页面 → 页面还活着（只是浏览器进程交接）" % pages)
            return True
        if time.time() - t0 >= probe:
            log("页面注册表已空 → 判定页面已关")
            return False
        time.sleep(poll)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
PORT = 8777


def main():
    global PORT
    argv = sys.argv[1:]
    force_tab = "--tab" in argv
    no_browser = "--no-browser" in argv
    do_stop = "--stop" in argv

    cfg = load_config()
    PORT = int(cfg.get("listen_port", 8777))
    app = cfg.get("app") or {}
    wcfg = app.get("window") or {}
    url = "http://127.0.0.1:%d" % PORT

    log("=" * 56)
    log("%s 启动器：dir=%s port=%d" % (APP_NAME, HERE, PORT))

    if do_stop:
        try:
            http_json("/api/app/quit", method="POST")
            log("已请求后端退出")
        except Exception as e:
            log("后端未在运行或请求失败：", e)
        try:
            os.remove(cdp_json_path())          # 端口记录作废
        except Exception:
            pass
        return

    if port_open(PORT):
        log("端口 %d 已在监听 → 复用已有后端，只开窗" % PORT)
        if no_browser:
            return
        open_browser(url, app_mode=not force_tab, geo=wcfg, acfg=app)
        return

    # --- 起后端（python + CREATE_NO_WINDOW：无黑框，但 stdout 依然有效） ---
    # ⚠️ 不能用 pythonw：pythonw 下 sys.stdout 是 None，comfy_studio.py 里的 print() 会直接抛异常。
    #    用 python.exe + CREATE_NO_WINDOW 会创建一个**隐藏的控制台**，既不闪黑框又有可用的 stdout。
    bindir = os.path.dirname(sys.executable)
    exe = os.path.join(bindir, "python.exe")
    if not os.path.isfile(exe):
        exe = sys.executable
    backend_py = os.path.join(HERE, "comfy_studio.py")
    env = dict(os.environ)
    env["COMFYSTUDIO_HOME"] = HOME
    env["COMFYSTUDIO_CONFIG"] = CONFIG_PATH
    env.pop("HTTP_PROXY", None)
    env.pop("HTTPS_PROXY", None)
    env["NO_PROXY"] = "localhost,127.0.0.1,::1"
    CREATE_NO_WINDOW = 0x08000000
    try:
        bp = subprocess.Popen([exe, backend_py], cwd=HERE, env=env,
                              creationflags=CREATE_NO_WINDOW, close_fds=True,
                              stdin=subprocess.DEVNULL)
    except Exception as e:
        log("!! 后端启动失败：", e)
        return
    log("后端进程 pid=%s (%s)" % (bp.pid, exe))

    if not wait_backend(90):
        log("!! 后端 90 秒未就绪，仍然尝试开窗（看 logs 目录排查）")
    else:
        log("后端就绪：%s" % url)

    if no_browser:
        log("--no-browser：后端已起，退出启动器")
        return

    proc, how = open_browser(url, app_mode=not force_tab, geo=wcfg, acfg=app)
    log("窗口方式：%s" % how)

    if proc is None:
        # 普通标签页：没法知道窗口何时关，靠后端心跳超时自杀 → 这里只守着后端
        while True:
            time.sleep(5)
            if bp.poll() is not None:
                log("后端已退出，启动器结束")
                return
    else:
        # App 模式：等窗口进程退出
        while True:
            bp.poll()
            if bp.poll() is not None:
                log("后端已退出，启动器结束")
                return
            if proc.poll() is not None:
                time.sleep(3)
                if page_alive():
                    log("浏览器进程退出了但页面还在打心跳 → 可能只是进程交接，继续等")
                    # 继续守着后端，交给心跳超时兜底
                    while bp.poll() is None:
                        time.sleep(5)
                    log("后端已退出，启动器结束")
                    return
                log("窗口已关闭 → 请求后端停引擎并退出")
                try:
                    http_json("/api/app/quit", method="POST")
                except Exception as e:
                    log("退出请求失败（继续等后端自己退）：", e)
                t0 = time.time()
                while bp.poll() is None and time.time() - t0 < 40:
                    time.sleep(0.5)
                if bp.poll() is None:
                    log("后端 40 秒未退 → 强制结束")
                    try:
                        subprocess.run(["taskkill", "/PID", str(bp.pid), "/T", "/F"],
                                       creationflags=0x08000000, capture_output=True, timeout=20)
                    except Exception:
                        pass
                log("启动器结束")
                return
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("!! 启动器异常：%s" % e)
        log(traceback.format_exc())
        raise
