"""权限自检 / 自动提权。

背景（实测）：明日方舟 PC 客户端由 Hypergryph Launcher **以管理员启动**。
非管理员进程去抢焦点、还原最小化窗口、SetCursorPos 都会被 Windows 静默拒绝
（`OpenProcess(PROCESS_ALL_ACCESS)` 直接 error 5）。表现就是「截图正常、日志说点了、
游戏纹丝不动」。所以 PC 端必须提权 —— 但模拟器用户不需要。

这里的策略：
  1) 启动时判断「这次运行到底需不需要管理员」；
  2) 需要且当前不是 → 用 ShellExecuteW("runas") 把自己重启成管理员（弹一次 UAC）；
  3) 用户拒绝 / 判断不了 → 面板上留一个「以管理员重启」按钮，随时补。
"""
import ctypes
import os
import subprocess
import sys

from .logger import get_logger

log = get_logger()

SW_SHOWNORMAL = 1
ERROR_CANCELLED = 1223


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _game_pid(cfg):
    """找到游戏窗口的进程号（找不到返回 0）。"""
    try:
        from .pc_client import PcClient
        info = PcClient(cfg).find_window()
        return info[3] if info else 0
    except Exception:
        return 0


def game_is_protected(cfg) -> bool:
    """游戏进程权限是否高于我们（= 需要管理员）。"""
    pid = _game_pid(cfg)
    if not pid:
        return False
    try:
        from .pc_client import process_accessible
        return not process_accessible(pid)
    except Exception:
        return False


def needs_elevation(cfg) -> bool:
    """这次运行需不需要管理员。"""
    if is_admin():
        return False
    try:
        from .device import backend_kind
        kind = backend_kind(cfg)
    except Exception:
        return False
    if kind != "pc":
        return False            # 模拟器 + ADB 不需要管理员
    return game_is_protected(cfg)


def relaunch_as_admin(extra_args=None) -> bool:
    """用 UAC 把自己重启成管理员。返回 True 表示已发起（旧进程应退出）。"""
    args = list(extra_args if extra_args is not None else sys.argv[1:])
    if "--elevated" not in args:
        args.append("--elevated")
    if getattr(sys, "frozen", False):
        exe = sys.executable
        workdir = os.path.dirname(exe)
    else:
        exe = sys.executable
        script = os.path.abspath(sys.argv[0])
        entry = os.path.join(os.path.dirname(script), "gui.py")
        args = [entry if os.path.exists(entry) else script] + args
        workdir = os.path.dirname(script)
    params = subprocess.list2cmdline(args)
    try:
        r = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, workdir, SW_SHOWNORMAL)
    except Exception as e:
        log.error("提权失败: %s", e)
        return False
    if r <= 32:
        if r == 0:
            log.warning("提权被取消（UAC 点了否）")
        else:
            log.warning("提权启动失败，ShellExecute 返回 %s", r)
        return False
    log.info("已用管理员身份重启（原窗口会关闭）")
    return True
