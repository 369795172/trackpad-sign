#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trackpad_sign.py — 把 MacBook 触摸板当绝对坐标签名板（Wacom 式手感）

用法:
    python3 trackpad_sign.py             # 启动守护进程 + 自动打开浏览器
    python3 trackpad_sign.py --no-open   # 不自动开浏览器
    python3 trackpad_sign.py --port 8790  # 换端口（默认 8766）

原理:
    dlopen 私有框架 MultitouchSupport，注册 MTRegisterContactFrameCallback，
    读取手指在板面上的归一化绝对坐标（0..1，y 向上）、接触状态
    （3=落下 4=接触中 5=抬起）和接触面积（size），经 SSE 推给 index.html 画布。
    开关 = 系统点按事件（CGEventTap 监听左键按下，轻点点按与物理按下均触发，
    需"输入监控"权限）：点按一下切换 书写/待命；书写中：触碰即落笔、抬起即收笔，
    手指落在板面哪里，笔就落在画布哪里；待命时轻滑、误触不落墨。
    无权限时降级为压力阈值模式（接触面积 ≥ 阈值视为切换）。

结构体来源:
    Karabiner-Elements src/apps/MultitouchExtension/src/MultitouchPrivate.h
    （96 字节/指，2009 → macOS 26 布局稳定；M3/M4 Apple Silicon 实证）

仅监听本机 127.0.0.1，无任何外部依赖（纯标准库）。

License: MIT
"""
import argparse
import ctypes
import json
import math
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
INDEX_PATH = HERE / "index.html"
MT_LIB_PATH = "/System/Library/PrivateFrameworks/MultitouchSupport.framework/MultitouchSupport"

# ---------- MultitouchSupport 结构体（Karabiner MultitouchPrivate.h，96 字节/指） ----------

class MTVector(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]

class MTReadout(ctypes.Structure):
    _fields_ = [("position", MTVector), ("velocity", MTVector)]

class MTFinger(ctypes.Structure):
    _fields_ = [
        ("frame", ctypes.c_int),          # +0
        ("timestamp", ctypes.c_double),   # +8
        ("identifier", ctypes.c_int),     # +16  稳定手指 ID（跨帧跟踪用）
        ("state", ctypes.c_int),          # +20  3=落下 4=接触中 5=抬起
        ("fingerId", ctypes.c_int),       # +24
        ("handId", ctypes.c_int),         # +28
        ("normalized", MTReadout),        # +32/+36 板面归一化坐标 (0..1, y 向上)
        ("size", ctypes.c_float),         # +48 接触面积 ~0.3(轻) → ~1.35(重按)
        ("pressure", ctypes.c_int),       # +52
        ("angle", ctypes.c_float),        # +56
        ("majorAxis", ctypes.c_float),    # +60
        ("minorAxis", ctypes.c_float),    # +64
        ("absoluteVector", MTReadout),    # +68 物理毫米坐标
        ("unknown1", ctypes.c_int * 2),   # +84
        ("zDensity", ctypes.c_float),     # +92
    ]

MT_ST_MAKE, MT_ST_TOUCHING, MT_ST_BREAK = 3, 4, 5
STRIDE = ctypes.sizeof(MTFinger)

CBTYPE = ctypes.CFUNCTYPE(
    ctypes.c_int,        # 返回值
    ctypes.c_void_p,     # device
    ctypes.c_void_p,     # fingers[]（n × 96 字节）
    ctypes.c_int,        # nFingers
    ctypes.c_double,     # timestamp
    ctypes.c_int,        # frame
)

# ---------- 运行时状态 ----------

clients = set()                    # 每个 SSE 订阅者一个 queue
clients_lock = threading.Lock()
HELLO = {"aspect_w": 121.9, "aspect_h": 74.1, "firm": 1.4, "mode": "pressure"}
_seen_frame = [False]
LAST_PING = [None]                # 页面聚焦心跳（monotonic）；开关只在页面活跃时生效
CLICK_TAP_OK = [False]            # 系统点按监听（CGEventTap）是否就绪

def _page_alive():
    return LAST_PING[0] is not None and time.monotonic() - LAST_PING[0] < 2.0

def broadcast(event, obj):
    data = "event: %s\ndata: %s\n\n" % (event, json.dumps(obj, separators=(",", ":")))
    with clients_lock:
        dead = []
        for q in clients:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            clients.discard(q)

class Pen:
    """开关 + 笔指状态机。

    开关（toggle）：系统点按事件（CGEventTap，主）或接触面积迟滞（无权限时备用）
    切换 书写/待命；350ms 冷却防连切、仅页面聚焦时响应（防日常点击误触）、
    切换帧不参与笔画（开关不留墨点）。
    书写中：落笔（make）→ 运笔（touching）→ 收笔（break/消失），
    以手指 identifier 跨帧跟踪，换手指必须经过一次抬笔。
    框架回调线程、点按监听线程与 selftest 注入线程并发调用，必须持锁。"""
    def __init__(self, firm_high=1.4):
        self.pid = None
        self.lock = threading.Lock()
        self.armed = False
        self.firm_high = float(firm_high)
        self.firm_low = self.firm_high * 0.6
        self.firm_latched = False
        self.last_toggle = 0.0
        self.last_tel = 0.0

    def feed(self, fingers):
        with self.lock:
            self._feed_locked(fingers)

    def set_firm(self, v):
        with self.lock:
            self.firm_high = float(v)
            self.firm_low = self.firm_high * 0.6
            return self.firm_high

    def _broadcast_armed(self):
        broadcast("armed", {"on": self.armed, "firm": round(self.firm_high, 2)})

    def _disarm_cleanup(self):
        if self.pid is not None:
            self.pid = None
            broadcast("pen", {"t": "up", "id": None})

    def toggle(self, allow):
        """切换 书写/待命。allow = 页面聚焦门控；350ms 冷却防连切。"""
        now = time.monotonic()
        with self.lock:
            if now - self.last_toggle <= 0.35 or not allow:
                return False
            self.armed = not self.armed
            self.last_toggle = now
            if not self.armed:
                self._disarm_cleanup()
            self._broadcast_armed()
            return True

    def _feed_locked(self, fingers):
        now = time.monotonic()
        touching = [f for f in fingers if f["state"] in (MT_ST_MAKE, MT_ST_TOUCHING, MT_ST_BREAK)]
        big = max(touching, key=lambda f: f["size"]) if touching else None
        did_toggle = False

        if not CLICK_TAP_OK[0]:  # 备用：无点按权限时用接触面积迟滞近似"重按"
            if big is not None:
                s = big["size"]
                if not self.firm_latched and s >= self.firm_high:
                    self.firm_latched = True
                    did_toggle = self.toggle(_page_alive())
                elif self.firm_latched and s < self.firm_low:
                    self.firm_latched = False
            else:
                self.firm_latched = False

        if now - self.last_tel > 0.05:
            self.last_tel = now
            if big is not None:
                broadcast("touch", {"x": _f(big["x"]), "y": _f(big["y"]),
                                    "s": _f(big["size"]), "p": big.get("pressure", 0)})
            else:
                broadcast("touch", None)

        if not self.armed:
            self._disarm_cleanup()
            return

        if did_toggle:
            return  # 切换帧不参与笔画：重按/点按开关本身不留墨点

        if self.pid is None:
            makes = [f for f in touching if f["state"] == MT_ST_MAKE]
            if makes:
                pen = max(makes, key=lambda f: f["size"])
                self.pid = pen["id"]
                broadcast("pen", {"t": "down", "id": self.pid,
                                  "x": _f(pen["x"]), "y": _f(pen["y"]), "s": _f(pen["size"])})
        else:
            cur = next((f for f in touching if f["id"] == self.pid), None)
            if cur is None:
                self.pid = None
                broadcast("pen", {"t": "up", "id": None})
            elif cur["state"] == MT_ST_BREAK:
                broadcast("pen", {"t": "move", "id": self.pid,
                                  "x": _f(cur["x"]), "y": _f(cur["y"]), "s": _f(cur["size"])})
                broadcast("pen", {"t": "up", "id": None})
                self.pid = None
            else:
                broadcast("pen", {"t": "move", "id": self.pid,
                                  "x": _f(cur["x"]), "y": _f(cur["y"]), "s": _f(cur["size"])})

def _f(v):
    return round(float(v), 5)

pen = Pen()

# 板面物理范围观测（absoluteVector 毫米坐标），用于校准画布纵横比
_mm = {"minx": 1e9, "miny": 1e9, "maxx": -1e9, "maxy": -1e9, "t": 0.0}

def _note_mm(f):
    x = f.absoluteVector.position.x
    y = f.absoluteVector.position.y
    if not (-50 < x < 300 and -50 < y < 300):
        return
    changed = False
    if x < _mm["minx"]: _mm["minx"] = x; changed = True
    if x > _mm["maxx"]: _mm["maxx"] = x; changed = True
    if y < _mm["miny"]: _mm["miny"] = y; changed = True
    if y > _mm["maxy"]: _mm["maxy"] = y; changed = True
    now = time.time()
    if changed and _mm["minx"] < 1e8 and now - _mm["t"] > 5:
        _mm["t"] = now
        print("[trackpad_sign] 观测板面范围(mm): x %.1f~%.1f, y %.1f~%.1f → 纵横比约 %.1f:%.1f "
              "（如与当前比例差得多，可用 --aspect W:H 重启）"
              % (_mm["minx"], _mm["maxx"], _mm["miny"], _mm["maxy"],
                 _mm["maxx"] - _mm["minx"], _mm["maxy"] - _mm["miny"]), flush=True)

def on_frame(dev, fingers, n, ts, frame):
    try:
        if not _seen_frame[0]:
            _seen_frame[0] = True
            print("[trackpad_sign] 已收到触摸数据流 ✓", flush=True)
        fs = []
        for i in range(n):
            f = MTFinger.from_address(fingers + i * STRIDE)
            _note_mm(f)
            fs.append({"id": f.identifier, "state": f.state,
                       "x": f.normalized.position.x, "y": f.normalized.position.y,
                       "size": f.size, "pressure": f.pressure})
        if _selftest_busy[0]:
            return 0  # 注入期间丢弃真实帧，保证链路测试确定性
        pen.feed(fs)
    except Exception as e:  # 回调里绝不能崩
        print("[cb error]", e, flush=True)
    return 0

_selftest_busy = [False]

def inject_selftest():
    """不碰触摸板即可验证整条链路（daemon → SSE → 画布）。
    用 id=31 避开框架真实手指 ID（通常 0..15）；注入期间屏蔽真实输入，
    临时进入书写态，结束后还原。"""
    time.sleep(0.15)
    _selftest_busy[0] = True
    with pen.lock:
        prev = pen.armed
        pen.armed = True
    pen._broadcast_armed()
    n = 48
    pen.feed([{"id": 31, "state": MT_ST_MAKE, "x": 0.18, "y": 0.45, "size": 0.55}])
    for i in range(1, n + 1):
        t = i / n
        x = 0.18 + 0.55 * t
        y = 0.45 + 0.16 * math.sin(t * 2.2 * math.pi) * (1 - t * 0.35)
        pen.feed([{"id": 31, "state": MT_ST_TOUCHING, "x": x, "y": y, "size": 0.55}])
        time.sleep(0.008)
    pen.feed([{"id": 31, "state": MT_ST_BREAK, "x": 0.73, "y": 0.45, "size": 0.45}])
    pen.feed([])
    with pen.lock:
        pen.armed = prev
    pen._broadcast_armed()
    _selftest_busy[0] = False
    print("[trackpad_sign] selftest 笔画已注入", flush=True)

# ---------- 系统点按监听（CGEventTap，主开关信号） ----------

_TAPCB = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                          ctypes.c_void_p, ctypes.c_void_p)

_CLICKS_SEEN = [0]
_FOCUS_NAGGED = [False]

def _on_click(proxy, etype, event, userinfo):
    try:
        _CLICKS_SEEN[0] += 1
        if _CLICKS_SEEN[0] <= 3:
            print("[trackpad_sign] 收到系统点按事件 ×%d（点按开关生效中）" % _CLICKS_SEEN[0], flush=True)
        if not _page_alive():
            if not _FOCUS_NAGGED[0]:
                _FOCUS_NAGGED[0] = True
                print("[trackpad_sign] 点按被忽略：签名页不在前台（或心跳未达）", flush=True)
            return None
        pen.toggle(True)
    except Exception:
        pass
    return None

_CLICK_CB_KEEP = _TAPCB(_on_click)  # 防 GC

def _tap_thread(cg, cf, common_modes):
    tap = None
    for attempt in range(6):
        tap = cg.CGEventTapCreate(0, 0, 1, 1 << 1, _CLICK_CB_KEEP, None)
        if tap:
            break
        if attempt == 0:
            print("[trackpad_sign] 点按模式申请中（若弹出「输入监控」授权请允许），8 秒后重试…", flush=True)
        time.sleep(8)
    if not tap:
        print("[trackpad_sign] 点按模式不可用 → 降级压力阈值模式（用力按下切换）。"
              "如需真·点按：系统设置 → 隐私与安全性 → 输入监控，允许本程序所属终端后重启。", flush=True)
        broadcast("mode", {"mode": "pressure"})
        return
    src = cf.CFMachPortCreateRunLoopSource(None, ctypes.c_void_p(tap), 0)
    cf.CFRunLoopAddSource(ctypes.c_void_p(cf.CFRunLoopGetCurrent()),
                          ctypes.c_void_p(src), common_modes)
    cg.CGEventTapEnable(ctypes.c_void_p(tap), True)
    CLICK_TAP_OK[0] = True
    print("[trackpad_sign] 点按模式已就绪：点按一下触摸板（轻点或物理按下）切换开始/结束。", flush=True)
    broadcast("mode", {"mode": "click"})
    cf.CFRunLoopRun()

def start_click_tap():
    try:
        cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    except OSError as e:
        print("[trackpad_sign] CoreGraphics/CoreFoundation 加载失败（%s）→ 压力阈值模式" % e, flush=True)
        return
    cg.CGEventTapCreate.restype = ctypes.c_void_p
    cg.CGEventTapCreate.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                    ctypes.c_uint64, _TAPCB, ctypes.c_void_p]
    cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
    cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
    cf.CFRunLoopRun.restype = None
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    common_modes = ctypes.c_void_p(cf.CFStringCreateWithCString(None, b"kCFRunLoopCommonModes", 0))
    threading.Thread(target=_tap_thread, args=(cg, cf, common_modes), daemon=True).start()

# ---------- HTTP / SSE ----------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                body = INDEX_PATH.read_bytes()
            except OSError:
                self.send_error(500, "index.html missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            q = queue.Queue(maxsize=512)
            with clients_lock:
                clients.add(q)
            try:
                self.connection.settimeout(5.0)  # 后台标签页停止读取导致写阻塞时，及时断开让其重连重同步
                hello_payload = dict(HELLO)
                hello_payload["mode"] = "click" if CLICK_TAP_OK[0] else "pressure"
                hello_payload["armed"] = pen.armed
                hello = "event: hello\ndata: %s\n\nretry: 1000\n\n" % json.dumps(hello_payload)
                self.wfile.write(hello.encode("utf-8"))
                self.wfile.flush()
                while True:
                    try:
                        item = q.get(timeout=15)
                    except queue.Empty:
                        item = ": ping\n\n"
                    self.wfile.write(item.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with clients_lock:
                    clients.discard(q)
        elif self.path == "/selftest":
            threading.Thread(target=inject_selftest, daemon=True).start()
            body = b"injected"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/ping":
            if LAST_PING[0] is None:
                print("[trackpad_sign] 页面聚焦心跳已收到", flush=True)
            LAST_PING[0] = time.monotonic()
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/toggle":
            ok = pen.toggle(True)
            body = json.dumps({"ok": ok, "armed": pen.armed}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/firm":
            try:
                length = int(self.headers.get("Content-Length", 0))
                v = json.loads(self.rfile.read(length) or b"{}").get("v")
            except Exception:
                v = None
            if isinstance(v, (int, float)) and 0.3 <= float(v) <= 6.0:
                pen.set_firm(float(v))
                broadcast("firm", {"firm": round(pen.firm_high, 2)})
                body = b'{"ok":true}'
            else:
                body = b'{"ok":false}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

# ---------- main ----------

def main():
    global HELLO
    ap = argparse.ArgumentParser(description="触摸板绝对坐标签名板守护进程")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--aspect", default=None, help="板面物理纵横比 W:H（毫米），如 160:105；缺省用探测值或 121.9:74.1")
    ap.add_argument("--firm", type=float, default=1.4, help="重按开关阈值：接触面积 ≥ 此值视为重按（页面压力读数可校准）")
    args = ap.parse_args()

    if STRIDE != 96:
        raise SystemExit("MTFinger 结构体大小异常：%d != 96，拒绝运行以免读到脏数据" % STRIDE)

    try:
        mt = ctypes.cdll.LoadLibrary(MT_LIB_PATH)
    except OSError as e:
        raise SystemExit("无法加载 MultitouchSupport：%s" % e)

    mt.MTDeviceCreateDefault.restype = ctypes.c_void_p
    dev = mt.MTDeviceCreateDefault()
    if not dev:
        raise SystemExit("MTDeviceCreateDefault 返回空（未找到内置触摸板？）")
    print("[trackpad_sign] 触摸板设备 = 0x%x" % dev, flush=True)

    aw = ah = 0.0
    try:
        get_dims = mt.MTDeviceGetSensorSurfaceDimensions
        get_dims.argtypes = [ctypes.c_void_p,
                             ctypes.POINTER(ctypes.c_float),
                             ctypes.POINTER(ctypes.c_float)]
        get_dims.restype = ctypes.c_bool
        w = ctypes.c_float(0.0)
        h = ctypes.c_float(0.0)
        ok = get_dims(ctypes.c_void_p(dev), ctypes.byref(w), ctypes.byref(h))
        if ok and 1 < w.value < 500 and 1 < h.value < 500:
            aw, ah = w.value, h.value
        else:
            print("[trackpad_sign] 板面尺寸探测不可用（ret=%s w=%s h=%s），改用缺省比例；"
                  "真实比例会在使用中打印，可用 --aspect W:H 精确指定"
                  % (ok, w.value, h.value), flush=True)
    except AttributeError:
        print("[trackpad_sign] 无 MTDeviceGetSensorSurfaceDimensions 符号，改用缺省比例", flush=True)
    if args.aspect:
        try:
            a, b = args.aspect.split(":")
            aw, ah = float(a), float(b)
        except Exception:
            raise SystemExit("--aspect 格式应为 W:H（毫米），如 160:105")
    if not (1 < aw < 500 and 1 < ah < 500):
        aw, ah = 121.9, 74.1
    pen.firm_high = args.firm
    pen.firm_low = pen.firm_high * 0.6
    HELLO = {"aspect_w": round(aw, 2), "aspect_h": round(ah, 2), "firm": round(pen.firm_high, 2)}

    global _CB_KEEP
    _CB_KEEP = CBTYPE(on_frame)  # 防 GC：回调对象必须长存活
    mt.MTRegisterContactFrameCallback.argtypes = [ctypes.c_void_p, CBTYPE]
    mt.MTRegisterContactFrameCallback(ctypes.c_void_p(dev), _CB_KEEP)
    mt.MTDeviceStart.argtypes = [ctypes.c_void_p, ctypes.c_int]
    mt.MTDeviceStart(ctypes.c_void_p(dev), 0)
    start_click_tap()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = "http://127.0.0.1:%d/" % args.port
    print("[trackpad_sign] 画布: %s （板面纵横比 %s:%s mm）" % (url, HELLO["aspect_w"], HELLO["aspect_h"]), flush=True)
    print("[trackpad_sign] 点按一下触摸板 = 开始书写；再点按一下 = 结束。轻滑、误触不落墨。", flush=True)
    print("[trackpad_sign] 开关只在签名页面处于前台时生效（防止日常点击误开关）。", flush=True)
    print("[trackpad_sign] 若触碰后仍无笔画：系统设置 → 隐私与安全性 → 输入监控，允许运行本程序的终端 App。", flush=True)

    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[trackpad_sign] 退出", flush=True)

if __name__ == "__main__":
    main()
