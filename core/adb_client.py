"""ADB 封装（subprocess 驱动）。

自动探测模拟器 ADB 端口；提供截图/点击/滑动/长按/分辨率/启停应用。

注意：`screencap -p` 返回的是**二进制 PNG**，必须走 bytes 通道，
不能用 text=True（Windows 下会按 GBK 解码直接炸）。见 _run_bin()。
"""
import os
import re
import shutil
import subprocess
import sys
import time

# 关键：打包成 windowed exe 后每次 subprocess 都会弹一个黑框再消失
# （用户看到的"一直弹出消失弹出消失"就是它）。CREATE_NO_WINDOW 彻底压掉。
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _no_window_kwargs():
    if _NO_WINDOW:
        return {"creationflags": _NO_WINDOW,
                "startupinfo": subprocess.STARTUPINFO()}
    return {}


def _apply_no_window(si):
    if _NO_WINDOW:
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0      # SW_HIDE
    return si

import cv2
import numpy as np

from .logger import get_logger

log = get_logger()

# 常见模拟器自带的 adb 路径（PATH 里没有 adb 时兜底）
ADB_HINTS = [
    # MuMu 15.x（新版目录结构：nx_main 里带 adb.exe，端口默认 16384）
    r"D:/Program Files/Netease/MuMu/nx_main/adb.exe",
    r"C:/Program Files/Netease/MuMu/nx_main/adb.exe",
    r"C:/Program Files/Netease/MuMuPlayer-15.0/nx_main/adb.exe",
    r"C:/Program Files/Netease/MuMu Player 12/shell/adb.exe",
    r"C:/Program Files/Netease/MuMuPlayer-12.0/shell/adb.exe",
    r"C:/Program Files/MuMu Player 12/shell/adb.exe",
    r"C:/Program Files/i4Tools9/files/adb/adb.exe",
    r"C:/leidian/LDPlayer9/adb.exe",
    r"C:/Program Files/BlueStacks_nxt/HD-Player.exe",
]


class AdbError(RuntimeError):
    pass


class AdbClient:
    def __init__(self, cfg):
        a = cfg["adb"]
        self.cfg = cfg
        self.adb = self._resolve_adb(a.get("adb_path") or "")
        self.host = a.get("host", "127.0.0.1")
        self.ports = a.get("ports", [16384, 7555, 5555])
        self.timeout = a.get("connect_timeout", 10)
        self.serial = None
        self._size = None

    # ---------- 启动前置 ----------
    def _resolve_adb(self, explicit: str) -> str:
        if explicit:
            if not os.path.exists(explicit):
                raise AdbError(f"config 里的 adb_path 不存在: {explicit}")
            return explicit
        found = shutil.which("adb")
        if found:
            return found
        for hint in ADB_HINTS:
            if os.path.exists(hint):
                log.info("PATH 中无 adb，使用模拟器自带: %s", hint)
                return hint
        raise AdbError(
            "找不到 adb 可执行文件。请安装 platform-tools 并加入 PATH，"
            "或在 config.yaml 的 adb.adb_path 填绝对路径"
        )

    # ---------- 底层执行 ----------
    def _run(self, *args, timeout: int = None) -> subprocess.CompletedProcess:
        """文本通道（adb devices / shell 命令这类 ASCII 输出）。"""
        cmd = [self.adb, *args]
        try:
            si = subprocess.STARTUPINFO()
            _apply_no_window(si)
            return subprocess.run(
                cmd, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=timeout or self.timeout,
                creationflags=_NO_WINDOW, startupinfo=si,
            )
        except FileNotFoundError as e:
            raise AdbError(f"无法执行 adb: {e}") from e
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时({args}): {e}") from e

    def _run_bin(self, *args, timeout: int = 8) -> bytes:
        """二进制通道（screencap 这种返回原始像素的）。

        超时压到 8s：以前 30s × 3 次重试 = 90s 干等，看起来就是"卡死"。
        """
        cmd = [self.adb, *args]
        try:
            si = subprocess.STARTUPINFO()
            _apply_no_window(si)
            r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               creationflags=_NO_WINDOW, startupinfo=si)
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 命令超时({args[3:]}): {e}") from e
        except OSError as e:
            raise AdbError(f"adb 执行失败: {e}") from e
        if r.returncode != 0:
            err = r.stderr.decode("utf-8", "replace").strip()
            raise AdbError(f"adb 失败({args[3:]}): {err}")
        return r.stdout

    def _sh(self, *args, timeout: int = 30):
        if not self.serial:
            raise AdbError("尚未连接设备")
        return self._run("-s", self.serial, "shell", *args, timeout=timeout)

    # ---------- 连接 ----------
    def connect(self) -> str:
        """依次尝试各端口，返回连接成功的 serial。已在 devices 里的优先。"""
        # 1) 已经有设备在线（MuMu/雷电常驻端口），直接用
        online = self._list_devices()
        for addr in online:
            log.info("发现已在线设备: %s", addr)
            self.serial = addr
            return addr

        # 2) 逐端口 connect
        for port in self.ports:
            addr = f"{self.host}:{port}"
            try:
                r = self._run("connect", addr)
                out = f"{r.stdout}{r.stderr}"
                if "connected" in out and "cannot" not in out:
                    if self._is_device(addr):
                        self.serial = addr
                        log.info("已连接模拟器: %s", addr)
                        return addr
            except AdbError as e:
                log.debug("端口 %s 连接异常: %s", port, e)
        raise AdbError(
            "未找到可用的模拟器 ADB 设备：请确认模拟器已启动、已开启 ADB 调试，"
            f"并检查 config.yaml 的 adb.ports（当前尝试: {self.ports}）"
        )

    def _list_devices(self) -> list:
        r = self._run("devices")
        out = []
        for line in r.stdout.splitlines()[1:]:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            addr, state = line.split("\t", 1)
            if state.strip() == "device":
                out.append(addr.strip())
        return out

    def _is_device(self, addr: str) -> bool:
        r = self._run("devices")
        return any(addr in line and "\tdevice" in line for line in r.stdout.splitlines())

    # ---------- 设备能力 ----------
    def wm_size(self) -> tuple:
        if self._size:
            return self._size
        r = self._sh("wm", "size")
        # 优先取 "Override size"（若改了分辨率），否则取 Physical size
        override = re.search(r"Override size:\s*(\d+)\s*x\s*(\d+)", r.stdout)
        physical = re.search(r"(\d+)\s*x\s*(\d+)", r.stdout)
        m = override or physical
        if not m:
            raise AdbError(f"无法读取分辨率: {r.stdout}{r.stderr}")
        self._size = (int(m.group(1)), int(m.group(2)))
        return self._size

    def _try_reconnect(self):
        try:
            self._run("connect", self.serial or f"{self.host}:{self.ports[0]}")
        except Exception:
            pass

    def alive(self, timeout: int = 4) -> bool:
        """设备还在不在（用来做无人值守时的健康检查，别等截图超时才反应）。"""
        try:
            r = self._run("-s", self.serial, "get-state", timeout=timeout)
            return "device" in (r.stdout or "")
        except AdbError:
            return False

    def screencap(self, retries: int = 2) -> np.ndarray:
        """返回 BGR numpy 数组截图。

        实测踩坑：MuMu 偶尔会吐一次空 stdout（切场景/加载峰值时），
        `exec-out screencap` 就报 adb 失败；之前直接抛异常会把整轮公招打断。
        这里带重连重试，避免把一次抖动当事故。
        """
        last = None
        for i in range(max(1, retries)):
            try:
                return self._screencap_once()
            except AdbError as e:
                last = e
                log.warning("截图失败(%d/%d): %s", i + 1, retries, e)
                if i < retries - 1:
                    self._try_reconnect()
                    time.sleep(0.6)
        raise last or AdbError("截图失败")

    def _screencap_raw(self):
        """`exec-out screencap`（无 -p）：16 字节头 + RGBA 原始像素，比 PNG 快 ~15%。

        实测（MuMu/1600x900）：PNG 774ms，raw 660ms —— 截图本身是现在最大的固定开销，
        所以宁可多点 100 行解析，也别每步重新截一张。
        """
        try:
            raw = self._run_bin("-s", self.serial, "exec-out", "screencap", timeout=8)
        except AdbError as e:
            log.debug("raw 截图失败，回落 PNG: %s", e)
            return None
        if not raw or len(raw) < 16:
            return None
        w, h, fmt = np.frombuffer(raw[:12], dtype=np.uint32)[:3]
        if not (0 < w <= 4096 and 0 < h <= 4096):
            return None
        body = raw[12:]
        need = int(w) * int(h) * 4
        if len(body) < need:
            return None
        arr = np.frombuffer(body[:need], dtype=np.uint8).reshape(int(h), int(w), 4)
        if int(fmt) in (1, 5):          # RGBA_8888 / BGRA_8888
            return arr[:, :, :3].copy()
        return arr[:, :, [2, 1, 0]].copy()

    def _screencap_once(self) -> np.ndarray:
        raw_img = self._screencap_raw()
        if raw_img is not None:
            return raw_img
        try:
            raw = self._run_bin("-s", self.serial, "exec-out", "screencap", "-p",
                                timeout=10)
        except AdbError as e:
            log.debug("PNG 截图失败，改走 /sdcard 中转: %s", e)
            raw = b""
        if not raw:
            img = None
        else:
            img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            # 某些模拟器 exec-out 会带 \r\n 转换，退回 /sdcard 再 pull
            log.debug("exec-out 解码失败，改用 /sdcard 截图中转")
            self._sh("screencap", "-p", "/sdcard/_ak_shot.png")
            local = os.path.join(os.environ.get("TEMP", "."), "_ak_shot.png")
            self._run("-s", self.serial, "pull", "/sdcard/_ak_shot.png", local)
            img = cv2.imread(local, cv2.IMREAD_COLOR)
        if img is None:
            raise AdbError("截图解码失败（两种方式都失败）")
        return img

    def tap(self, x, y):
        self._sh("input", "tap", str(int(x)), str(int(y)))

    def tap_many(self, points, gap_ms: int = 20):
        """一次 adb 往返点很多下（每下还是在设备内单独 input，但省掉往返开销）。

        实测：8 次「+」分开点 ≈ 1.0~1.3s（8 次 adb 往返），
        合并成一条 shell ≈ 0.55s。设 9 小时就是这一条命令。
        """
        if not points:
            return
        parts = []
        for x, y in points:
            parts.append("input tap %d %d" % (int(x), int(y)))
            if gap_ms:
                parts.append("sleep %.2f" % (gap_ms / 1000.0))
        if parts and parts[-1].startswith("sleep"):
            parts.pop()
        self._sh(";".join(parts), timeout=30)

    def swipe(self, x1, y1, x2, y2, duration_ms: int = 300):
        self._sh("input", "swipe", str(int(x1)), str(int(y1)),
                 str(int(x2)), str(int(y2)), str(int(duration_ms)))

    def long_press(self, x, y, duration_ms: int = 800):
        # 同点 swipe = 长按
        self.swipe(x, y, x, y, duration_ms)

    def keyevent(self, code):
        self._sh("input", "keyevent", str(code))

    def back(self):
        self.keyevent(4)

    # ---------- 应用 ----------
    def current_package(self) -> str:
        r = self._sh("dumpsys", "window")
        m = re.search(r"mCurrentFocus=.*?\s([\w\.]+)/", r.stdout)
        return m.group(1) if m else ""

    def start_app(self, package: str, activity: str = "") -> None:
        comp = f"{package}/{activity}" if activity else None
        if comp:
            r = self._sh("am", "start", "-n", comp)
        else:
            r = self._sh("monkey", "-p", package, "-c",
                         "android.intent.category.LAUNCHER", "1")
        out = f"{r.stdout}{r.stderr}"
        if "Error" in out or "No activities found" in out:
            raise AdbError(f"启动 {package} 失败: {out.strip()}")
        log.info("已启动应用: %s", package)

    def app_running(self, package: str) -> bool:
        r = self._sh("pidof", package)
        return bool(r.stdout.strip())
