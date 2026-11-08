"""电脑端（官方 PC 客户端 / Unity 窗口）设备后端。

和 `AdbClient` 暴露**完全相同的鸭子接口**，因此 `core/ui.py` 一行都不用改：

    connect() -> str           # 返回 serial 字符串（"pc:<hwnd>"）
    screencap() -> np.ndarray  # BGR，尺寸 == 客户区尺寸
    tap(x, y)                  # 客户区像素坐标（左上原点）
    tap_many(points, gap_ms)
    swipe / long_press
    wm_size() -> (w, h)        # 客户区尺寸
    alive() -> bool
    current_package() -> str   # 前台是游戏窗口时返回 config.game.package
    start_app(pkg, activity)
    app_running(pkg) / kill_app(pkg)

坐标一律是「客户区像素」，和 ADB 的 input tap 语义一致 —— 这是移植能零成本的关键。

依赖：纯 ctypes（Win32 / GDI / SendInput），不装额外包也能跑；
      装了 mss 或 dxcam 会自动用更快的抓屏路径。
"""
import ctypes
import ctypes.wintypes as wt
import os
import re
import subprocess
import sys
import time

import numpy as np

from .logger import get_logger

log = get_logger()

IS_WIN = sys.platform == "win32"

if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:                                   # 非 Windows 只做导入兜底，import 不炸
    user32 = gdi32 = kernel32 = None

# ---------------------------------------------------------------- Win32 常量
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0
PW_CLIENTONLY = 1
PW_RENDERFULLCONTENT = 2
SW_RESTORE = 9
HWND_TOP = 0
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_ACTIVATE = 0x0006
WA_ACTIVE = 1
MK_LBUTTON = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

DEFAULT_TITLES = ["明日方舟", "Arknights", "arknights", "Arknights.exe"]
DEFAULT_CLASS = "UnityWndClass"


class PcError(RuntimeError):
    pass


# ---------------------------------------------------------------- 结构体
class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD),
                ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
                ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def _set_dpi_aware():
    """不做 DPI aware 的话，系统会把窗口坐标按 125%/150% 缩放虚拟化，
    截图尺寸和 GetClientRect 对不上，点哪偏哪 —— 这是 PC 端 90% 的「点歪」根因。"""
    if not IS_WIN:
        return
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass



# ---------------------------------------------------------------- 权限/反作弊体检
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def process_accessible(pid: int) -> bool:
    """能不能拿完整权限打开目标进程。

    False = 游戏进程比我们「权限高」（管理员启动 / 带反作弊驱动）：
    Windows 的 UIPI 会把我们发过去的鼠标消息、SetForegroundWindow、
    ShowWindow 全部静默丢弃 —— 现象就是「截图正常、点了没反应」。
    解药只有一个：脚本也用管理员跑。
    """
    try:
        h = kernel32.OpenProcess(0x1F0FFF, False, pid)
        if not h:
            return False
        kernel32.CloseHandle(h)
        return True
    except Exception:
        return False


class PcClient:
    """官方 PC 客户端（或任何 Unity 窗口）的截图 / 点击后端。"""

    def __init__(self, cfg):
        if not IS_WIN:
            raise PcError("PC 后端只能在 Windows 上运行")
        _set_dpi_aware()
        self.cfg = cfg
        p = cfg.get("pc") or {}
        g = cfg.get("game") or {}
        self.exe_path = p.get("exe_path") or ""
        self.launcher_path = p.get("launcher_path") or ""
        self.process_name = (p.get("process_name") or "Arknights.exe").lower()
        self.title_kw = [s.lower() for s in (p.get("window_title_kw") or DEFAULT_TITLES)]
        self.window_class = p.get("window_class") or DEFAULT_CLASS
        self.capture = (p.get("capture") or "auto").lower()
        self.input_mode = (p.get("input") or "sendinput").lower()
        self.focus_required = bool(p.get("focus_required", True))
        self.hold_ms = int(p.get("click_hold_ms", 16))
        self.restore_cursor = bool(p.get("restore_cursor", True))
        # 屏幕抓取只能拍到「最前面」的东西：面板/微信/终端盖在游戏上就会抓到它们
        # （实测 19:07 那次就是这么读到自己面板的日志文字 → 认不出出货卡 → 狂点中央）
        self.focus_before_capture = bool(p.get("focus_before_capture", True))
        self._last_focus_ts = 0.0
        self.startup_timeout = int(p.get("startup_timeout", g.get("startup_timeout", 120)))
        self.pkg = g.get("package", "com.hypergryph.arknights")   # 给 ui.in_game() 用
        self.serial = None
        self.hwnd = None
        self._size = None
        self._origin = (0, 0)
        self._cursor0 = None
        self._sct = None
        self._cam = None

    # ------------------------------------------------------------ 窗口枚举
    def _pid_exe(self, pid: int) -> str:
        try:
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return ""
            buf = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            kernel32.CloseHandle(h)
            return buf.value if ok else ""
        except Exception:
            return ""

    def list_windows(self) -> list:
        """枚举所有可见顶层窗口：[(hwnd, title, class, pid, exe, w, h)]"""
        out = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            tbuf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, tbuf, n + 1)
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            r = RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            w, h = r.right - r.left, r.bottom - r.top
            iconic = bool(user32.IsIconic(hwnd))
            # 最小化时 GetWindowRect 给的是 -32000 的假坐标，别按尺寸过滤掉了
            if not iconic and (w < 200 or h < 120):
                return True
            out.append((hwnd, tbuf.value, cbuf.value, pid.value,
                        self._pid_exe(pid.value), w, h))
            return True

        user32.EnumWindows(_cb, 0)
        return out

    def find_window(self):
        cands = []
        for hwnd, title, cls, pid, exe, w, h in self.list_windows():
            exe_name = os.path.basename(exe).lower()
            by_exe = bool(self.process_name) and exe_name == self.process_name
            by_title = any(k in title.lower() for k in self.title_kw)
            by_class = cls == self.window_class
            if by_exe or (by_title and (by_class or not self.window_class)):
                cands.append((hwnd, title, cls, pid, exe, w, h, by_exe))
            elif by_title and by_class:
                cands.append((hwnd, title, cls, pid, exe, w, h, False))
        if not cands:
            return None
        # 优先：进程名精确命中 且 最大窗口（启动器/浮窗会小）
        cands.sort(key=lambda c: (c[7], c[5] * c[6]), reverse=True)
        return cands[0]

    def _bind(self, hwnd) -> None:
        self.hwnd = hwnd
        self._refresh_geometry()

    def _ensure_restored(self, wait: float = 1.5):
        """最小化时客户区是 0x0（GetWindowRect 还给 -32000），截图/点击全废。

        实测：对游戏窗口 `ShowWindow(SW_RESTORE)` 经常当场返回 0、IsIconic 还是 1，
        要几百毫秒之后才真的还原（窗口线程在忙）。所以这里用 ShowWindowAsync
        再轮询客户区，等到真的 >100px 为止。
        """
        if not self.hwnd:
            return
        cr = RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(cr))
        if cr.right > 100 and cr.bottom > 100:
            return
        try:
            user32.ShowWindowAsync(self.hwnd, SW_RESTORE)
        except Exception:
            user32.ShowWindow(self.hwnd, SW_RESTORE)
        end = time.time() + wait
        while time.time() < end:
            user32.GetClientRect(self.hwnd, ctypes.byref(cr))
            if cr.right > 100 and cr.bottom > 100:
                time.sleep(0.15)          # 等一帧渲染
                return
            time.sleep(0.1)

    def _refresh_geometry(self):
        self._ensure_restored()
        cr = RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(cr))
        w, h = cr.right - cr.left, cr.bottom - cr.top
        pt = POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        self._size = (w, h)
        self._origin = (pt.x, pt.y)
        self.serial = f"pc:{int(self.hwnd)}"

    # ------------------------------------------------------------ 连接 / 生命周期
    def connect(self) -> str:
        for i in range(3):
            info = self.find_window()
            if info:
                self._bind(info[0])
                log.info("已绑定窗口: 「%s」class=%s %dx%d (hwnd=%s)",
                         info[1], info[2], self._size[0], self._size[1], info[0])
                self._start_capture_backend()
                if not process_accessible(info[3]) and not is_admin():
                    msg = ("游戏进程权限高于本脚本（客户端是管理员启动的）→ 抢焦点、还原最小化窗口、"
                           "光标定位都会被系统静默拒绝。请「以管理员身份运行」。")
                    if (self.cfg.get("pc") or {}).get("require_admin", True):
                        raise PcError(msg + "（确定要硬跑可设 pc.require_admin: false）")
                    log.warning(msg)
                return self.serial
            log.warning("没找到游戏窗口（第 %d/3 次），若游戏没开先启动", i + 1)
            time.sleep(1.0)
        raise PcError(
            "找不到明日方舟 PC 客户端窗口。请确认：\n"
            "  1) 游戏已进入主界面（不是启动器）；\n"
            "  2) config.yaml → pc.exe_path / pc.process_name / pc.window_title_kw 正确；\n"
            "  3) 跑 `python pc_probe.py --list` 看实际窗口标题/类名/进程，抄进配置。"
        )

    def alive(self, timeout: int = 4) -> bool:
        if not self.hwnd or not user32.IsWindow(self.hwnd):
            return False
        if not user32.IsWindowVisible(self.hwnd):
            return False
        # WM_NULL 探活：卡死（未响应）的 Unity 窗口会超时
        res = ctypes.c_size_t(0)
        ok = user32.SendMessageTimeoutW(self.hwnd, 0, 0, 0, 0x0002, timeout * 1000,
                                       ctypes.byref(res))
        return bool(ok)

    def app_running(self, package: str = "") -> bool:
        return self.find_window() is not None

    def current_package(self) -> str:
        """前台窗口是游戏时返回 pkg —— 直接兼容 ui.in_game() 的判定逻辑。"""
        try:
            fg = user32.GetForegroundWindow()
            if not fg:
                return ""
            root = user32.GetAncestor(fg, 2)          # GA_ROOT
            if self.hwnd and root == self.hwnd:
                return self.pkg
            # 全屏 / 无边框时前台可能是子窗口，退一步比标题
            n = user32.GetWindowTextLengthW(fg)
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(fg, buf, n + 1)
            if any(k in buf.value.lower() for k in self.title_kw):
                return self.pkg
            return buf.value or ""
        except Exception:
            return ""

    def start_app(self, package: str = "", activity: str = "") -> None:
        """exe 没跑就拉起来；已经在跑就只把窗口提到前台。"""
        if self.find_window():
            log.info("游戏窗口已存在 → 提到前台")
            self._focus()
            return
        target = self.launcher_path or self.exe_path
        if not target or not os.path.exists(target):
            raise PcError(f"pc.exe_path / pc.launcher_path 不存在: {target!r}")
        log.info("启动 PC 客户端: %s", target)
        subprocess.Popen([target], cwd=os.path.dirname(target) or None,
                         creationflags=0x00000008)      # DETACHED_PROCESS
        end = time.time() + self.startup_timeout
        while time.time() < end:
            info = self.find_window()
            if info:
                self._bind(info[0])
                self._start_capture_backend()
                log.info("窗口已就绪: %dx%d", *self._size)
                return
            time.sleep(1.0)
        raise PcError(f"等窗口超时（{self.startup_timeout}s）")

    def kill_app(self, package: str = "") -> None:
        """对应 ADB 的 am force-stop：杀掉窗口所属进程。"""
        try:
            info = self.find_window()
            if info:
                log.warning("kill 游戏进程 pid=%s（%s）", info[3], info[4])
                subprocess.run(["taskkill", "/F", "/PID", str(info[3])],
                               capture_output=True, creationflags=0x08000000)
            else:
                subprocess.run(["taskkill", "/F", "/IM", self.process_name],
                               capture_output=True, creationflags=0x08000000)
        except Exception as e:
            log.warning("kill_app 失败: %s", e)
        self.hwnd = None
        self.serial = None

    # ------------------------------------------------------------ 几何
    def wm_size(self) -> tuple:
        if not self.hwnd:
            raise PcError("未连接窗口")
        self._refresh_geometry()
        return self._size

    def _to_screen(self, x, y) -> tuple:
        self._refresh_geometry()
        return int(self._origin[0] + x), int(self._origin[1] + y)

    # ------------------------------------------------------------ 抓屏
    def _start_capture_backend(self):
        self._sct = None
        self._cam = None
        if self.capture in ("auto", "mss", "screen"):
            try:
                import mss
                self._sct = mss.mss()
                if self.capture == "auto":
                    log.info("抓屏后端: mss（屏幕抓取，游戏需在前台）")
                return
            except Exception as e:
                if self.capture in ("mss", "screen"):
                    raise PcError(f"capture={self.capture} 需要 mss：pip install mss") from e
                log.warning("没装 mss，回落 PrintWindow（部分显卡驱动下会全黑）：pip install mss")
        if self.capture == "dxcam":
            try:
                import dxcam
                self._cam = dxcam.create(output_color="BGR")
                log.info("抓屏后端: dxcam")
            except Exception as e:
                log.warning("dxcam 不可用(%s) → 回落 PrintWindow", e)

    def _grab_win32(self, printwindow: bool = True) -> np.ndarray:
        hwnd = self.hwnd
        if user32.IsIconic(hwnd):                 # 最小化 → 客户区是 0x0，先还原
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.25)
        wr = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(wr))
        W, H = wr.right - wr.left, wr.bottom - wr.top
        if W <= 0 or H <= 0:
            raise PcError("窗口尺寸非法（最小化？）")
        hdc_win = user32.GetWindowDC(hwnd)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_win, W, H)
        gdi32.SelectObject(hdc_mem, hbmp)
        ok = False
        if printwindow:
            # PW_RENDERFULLCONTENT：Unity/D3D11 走这条才不是黑屏
            ok = bool(user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT))
        if not ok:
            gdi32.BitBlt(hdc_mem, 0, 0, W, H, hdc_win, 0, 0, SRCCOPY)
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = W
        bi.bmiHeader.biHeight = -H            # 负数 = 自上而下
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(W * H * 4)
        gdi32.GetDIBits(hdc_mem, hbmp, 0, H, buf, ctypes.byref(bi), DIB_RGB_COLORS)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)

        arr = np.frombuffer(buf.raw, dtype=np.uint8).reshape(H, W, 4)[:, :, :3]
        # 窗口矩形 → 客户区：Unity 窗口一般无边框，有边框时这里对正
        self._refresh_geometry()
        cw, ch = self._size
        ox = self._origin[0] - wr.left
        oy = self._origin[1] - wr.top
        img = arr[max(0, oy):oy + ch, max(0, ox):ox + cw]
        if img.shape[0] != ch or img.shape[1] != cw:
            import cv2
            img = cv2.resize(img, (cw, ch), interpolation=cv2.INTER_NEAREST)
        return np.ascontiguousarray(img)

    def _grab_screen(self) -> np.ndarray:
        self._ensure_restored()
        self._refresh_geometry()
        w, h = self._size
        l, t = self._origin
        if self._cam is not None:
            f = self._cam.grab(region=(l, t, l + w, t + h))
            if f is None:
                f = self._cam.grab(region=(l, t, l + w, t + h))
            if f is not None:
                return np.ascontiguousarray(f)
        if self._sct is not None:
            shot = self._sct.grab({"left": l, "top": t, "width": w, "height": h})
            return np.ascontiguousarray(np.asarray(shot)[:, :, :3])
        # 纯 Win32 兜底：桌面 DC BitBlt（要求窗口在前台且没被遮挡）
        return self._grab_win32(printwindow=False)

    def _ensure_front_for_capture(self):
        """抓屏前把游戏顶到最前（mss 抓的是屏幕，被挡住就拍到别的窗口）。"""
        if not (self.focus_before_capture and self.focus_required and self.hwnd):
            return
        now = time.time()
        if now - self._last_focus_ts < 0.30:      # 限流，别每帧都折腾焦点
            return
        self._last_focus_ts = now
        try:
            if user32.GetForegroundWindow() != self.hwnd:
                self._focus(tries=2)
        except Exception as e:
            log.debug("抓屏前提焦点失败: %s", e)

    def screencap(self, retries: int = 2) -> np.ndarray:
        last = None
        for i in range(max(1, retries)):
            try:
                if not self.hwnd or not user32.IsWindow(self.hwnd):
                    self.connect()
                self._refresh_geometry()
                self._ensure_front_for_capture()
                mode = self.capture
                if mode in ("printwindow", "gdi"):
                    img = self._grab_win32(True)
                elif mode in ("dxcam", "mss", "screen"):
                    img = self._grab_screen()
                else:                                  # auto
                    img = self._grab_screen()
                    if img is None or img.size == 0:
                        img = self._grab_win32(True)
                if img is not None and img.size:
                    return img
                raise PcError("抓到空帧")
            except Exception as e:
                last = e
                log.warning("截图失败(%d/%d): %s", i + 1, retries, e)
                if i < retries - 1:
                    time.sleep(0.4)
                    try:
                        self.connect()
                    except Exception:
                        pass
        raise PcError(
            "截图失败: %s | 排查：1) 窗口最小化且还原被拒(游戏是管理员启动的)"
            "→ 以管理员运行本脚本，或手动点任务栏放出窗口；2) 被别的窗口挡住 → "
            "脚本会自动提前台，提不动同样说明权限不够；3) 全屏独占 → 改窗口化；"
            "4) PrintWindow 全黑 → pip install mss" % last
        )

    # ------------------------------------------------------------ 输入
    def _focus(self, tries: int = 3):
        """把游戏窗口提到前台。

        后台进程直接 SetForegroundWindow 会被系统前台锁拦掉（实测：焦点留在微信，
        点击全部打在聊天窗上）。这里按「解锁前台锁」的标准套路做三连：
          a) 敲一下 ALT + 发一条 0 位移鼠标事件 → 本进程成为「最后输入来源」，不再被拦；
          b) AttachThreadInput 把本线程挂到当前前台线程上，再 SetForegroundWindow；
          c) 还不行就走 SwitchToThisWindow（未文档化，但最容易成功）。
        每次都用 GetForegroundWindow 验证，不靠猜。
        """
        if not self.focus_required:
            return True
        hwnd = self.hwnd
        if user32.GetForegroundWindow() == hwnd:
            return True
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        for i in range(max(1, tries)):
            # a) 让自己成为最后输入来源 + 释放 ALT 菜单锁
            user32.keybd_event(0x12, 0, 0, 0)          # ALT down
            user32.keybd_event(0x12, 0, 2, 0)          # ALT up
            self._mouse_event(MOUSEEVENTF_MOVE, 0, 0)

            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            my_tid = kernel32.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != my_tid:
                attached = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(my_tid, fg_tid, False)

            if user32.GetForegroundWindow() != hwnd:
                try:
                    user32.SwitchToThisWindow(hwnd, True)
                except Exception:
                    pass
            if user32.GetForegroundWindow() == hwnd:
                if i:
                    log.debug("第 %d 次尝试才拿到焦点", i + 1)
                return True
            time.sleep(0.12)

        log.warning("拿不到游戏窗口焦点（当前前台 hwnd=%s）→ 点击可能落到别的窗口。"
                    "先用 pc.input=postmessage 或手动点一下游戏窗口再跑",
                    user32.GetForegroundWindow())
        return False

    def _mouse_event(self, flags: int, dx: int = 0, dy: int = 0):
        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.mi = MOUSEINPUT(int(dx), int(dy), 0, flags, 0, 0)
        return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _set_cursor(self, sx, sy):
        """把鼠标挪到屏幕坐标。

        踩坑：`SetCursorPos` 在受限会话 / 某些反作弊注入环境下**静默失败**（返回 0，
        最后错误 0），于是点击落到旧光标位置 —— 表现就是「脚本说点了，游戏没反应」。
        这里先 SetCursorPos，读回来对不上就改用 SendInput 绝对坐标，再校验一次。
        """
        sx, sy = int(sx), int(sy)
        p = POINT()
        user32.SetCursorPos(sx, sy)
        user32.GetCursorPos(ctypes.byref(p))
        if abs(p.x - sx) <= 2 and abs(p.y - sy) <= 2:
            return True
        sw = user32.GetSystemMetrics(0) or 1920
        sh = user32.GetSystemMetrics(1) or 1080
        for _ in range(2):
            self._mouse_event(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                              round(sx * 65535 / max(1, sw - 1)),
                              round(sy * 65535 / max(1, sh - 1)))
            user32.GetCursorPos(ctypes.byref(p))
            if abs(p.x - sx) <= 2 and abs(p.y - sy) <= 2:
                return True
        log.warning("鼠标没挪到位：期望 (%s,%s) 实际 (%s,%s)", sx, sy, p.x, p.y)
        return False

    def _post_click(self, x, y):
        """后台点击：不抢鼠标、不抢焦点。Unity 部分版本只吃 Raw Input，
        这条路径不一定生效 —— `python pc_probe.py --click 0.5 0.5` 一试就知道。"""
        lp = (int(y) << 16) | (int(x) & 0xFFFF)
        user32.PostMessageW(self.hwnd, WM_ACTIVATE, WA_ACTIVE, 0)
        user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, 0, lp)
        user32.PostMessageW(self.hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(self.hold_ms / 1000.0)
        user32.PostMessageW(self.hwnd, WM_LBUTTONUP, 0, lp)

    def _send_click(self, sx, sy):
        if self.restore_cursor and self._cursor0 is None:
            p = POINT()
            user32.GetCursorPos(ctypes.byref(p))
            self._cursor0 = (p.x, p.y)
        if not self._set_cursor(sx, sy):
            # 光标没到位就绝对不许按！否则按在旧位置上 —— 在寻访/购买页面
            # 这种误触要花真金白银（合成玉 / 寻访十连），比"这一步失败"严重得多。
            log.error("光标定位失败，放弃这次点击 (%s,%s)", sx, sy)
            return False
        self._mouse_event(MOUSEEVENTF_LEFTDOWN)
        time.sleep(self.hold_ms / 1000.0)
        self._mouse_event(MOUSEEVENTF_LEFTUP)
        return True

    def tap(self, x, y):
        if not self.hwnd:
            raise PcError("未连接窗口")
        if self.input_mode == "postmessage":
            self._post_click(x, y)
            return
        # 拿不到焦点就**不点**：2026-11-08 实机，焦点被微信抢走后脚本照样发
        # SendInput，结果点进了别的窗口；更糟的是焦点半途被抢时，点击会落在
        # 屏幕同一坐标上的任意窗口（寻访/购买页面会花真金白银）。
        if not self._focus():
            log.error("没拿到游戏焦点 → 放弃这次点击 (%s,%s)", x, y)
            return
        sx, sy = self._to_screen(x, y)
        self._send_click(sx, sy)

    def tap_many(self, points, gap_ms: int = 20):
        """一条命令连点（对应 ADB 的 `input tap;sleep;input tap`）。"""
        if not points:
            return
        if self.input_mode == "postmessage":
            for x, y in points:
                self._post_click(x, y)
                time.sleep(gap_ms / 1000.0)
            return
        if not self._focus():
            log.error("没拿到游戏焦点 → 放弃这一串连点（%d 下）", len(points))
            return
        for x, y in points:
            sx, sy = self._to_screen(x, y)
            self._send_click(sx, sy)
            time.sleep(max(0, gap_ms) / 1000.0)

    def swipe(self, x1, y1, x2, y2, duration_ms: int = 300):
        if self.input_mode == "postmessage":
            steps = max(2, duration_ms // 16)
            self._post_click(x1, y1)
            for i in range(1, steps + 1):
                x = int(x1 + (x2 - x1) * i / steps)
                y = int(y1 + (y2 - y1) * i / steps)
                lp = (y << 16) | (x & 0xFFFF)
                user32.PostMessageW(self.hwnd, WM_MOUSEMOVE, MK_LBUTTON, lp)
                time.sleep(duration_ms / 1000.0 / steps)
            lp = (int(y2) << 16) | (int(x2) & 0xFFFF)
            user32.PostMessageW(self.hwnd, WM_LBUTTONUP, 0, lp)
            return
        self._focus()
        sx1, sy1 = self._to_screen(x1, y1)
        sx2, sy2 = self._to_screen(x2, y2)
        self._set_cursor(sx1, sy1)
        self._mouse_event(MOUSEEVENTF_LEFTDOWN)
        steps = max(4, duration_ms // 16)
        for i in range(1, steps + 1):
            self._set_cursor(sx1 + (sx2 - sx1) * i / steps,
                             sy1 + (sy2 - sy1) * i / steps)
            time.sleep(duration_ms / 1000.0 / steps)
        self._mouse_event(MOUSEEVENTF_LEFTUP)

    def long_press(self, x, y, duration_ms: int = 800):
        self.swipe(x, y, x, y, duration_ms)

    def keyevent(self, code):
        """极少用；PC 端没有 Android keycode，这里只处理 4(BACK)=ESC。"""
        if int(code) == 4:
            self.key_press(0x1B)

    def key_press(self, vk: int):
        self._focus()
        user32.keybd_event(vk, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(vk, 0, 2, 0)

    def back(self):
        self.key_press(0x1B)          # ESC

    # ------------------------------------------------------------ 一键体检
    def diagnose(self) -> list:
        """返回 [(级别, 文本)]，级别 ∈ OK/WARN/FAIL。pc_probe.py --diag 用。"""
        out = []
        out.append(("OK" if is_admin() else "WARN",
                    f"管理员权限: {'是' if is_admin() else '否（游戏是管理员启动时会点不动）'}"))

        info = self.find_window()
        if not info:
            out.append(("FAIL", "没找到游戏窗口：游戏没进主界面 / 标题名不对（跑 --list 看真名）"))
            return out
        hwnd, title, cls, pid, exe, w, h, by_exe = info
        out.append(("OK", f"窗口: 「{title}」 class={cls} pid={pid}"))
        out.append(("OK", f"可执文件: {exe}"))

        acc = process_accessible(pid)
        out.append(("OK" if acc else "FAIL",
                    f"游戏进程权限: {'可访问' if acc else '受保护/更高权限 → 脚本必须「以管理员身份运行」'}"))

        if not self.hwnd:
            self._bind(hwnd)
        w, h = self.wm_size()
        out.append(("OK" if w > 0 else "FAIL",
                    f"客户区: {w}x{h}" + ("" if w > 0 else " → 窗口最小化且还原失败，手动点一下任务栏")))
        if (w, h) != (1600, 900):
            out.append(("WARN", f"客户区不是 1600x900 → 极速路径(ui.fast)会退回慢速全屏 OCR；"
                                f"把游戏窗口调成 1600x900 可直接复用现成标定"))

        try:
            t0 = time.time()
            img = self.screencap()
            ms = (time.time() - t0) * 1000
            mean = float(img.mean())
            out.append(("OK" if ms < 60 else "WARN", f"抓屏: {ms:.0f}ms（ADB 约 660ms）"))
            out.append(("OK" if mean > 3 else "FAIL",
                        f"画面亮度: {mean:.1f}" + ("" if mean > 3 else " → 全黑，装 mss 或换 capture: gdi")))
        except Exception as e:
            out.append(("FAIL", f"抓屏: {str(e).splitlines()[0][:120]}"))
            out.append(("FAIL", "窗口最小化且还原被拒 → 手动把游戏窗口放出来（或管理员运行）"))
            return out

        got = self._focus()
        fg = user32.GetForegroundWindow()
        out.append(("OK" if got else "FAIL",
                    f"抢焦点: {'成功' if got else '失败 → UIPI 拦截（用管理员跑）或游戏窗口在别的桌面'}"))
        if got and fg == self.hwnd:
            out.append(("OK", "前台 == 游戏窗口，SendInput 点击才能落到游戏里"))
        return out
