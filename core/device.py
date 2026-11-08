"""设备后端工厂：同一套 UI 流程，跑在「模拟器/手机(ADB)」或「官方 PC 客户端」上。

配置：
    backend: auto        # auto | adb | pc

auto 的判定顺序：
    1) config 里 pc.exe_path 存在且当前能找到游戏窗口 → PC
    2) 否则走 ADB（原逻辑不变）

两个后端暴露相同的鸭子接口（screencap / tap / tap_many / swipe / wm_size /
alive / current_package / start_app / app_running / kill_app / connect），
所以 core/ui.py、core/runner.py 的流程代码**完全不用改**。
"""
from .logger import get_logger

log = get_logger()


def backend_kind(cfg) -> str:
    kind = str((cfg.get("backend") or "auto")).lower()
    if kind in ("adb", "android", "emulator"):
        return "adb"
    if kind in ("pc", "windows", "win", "client"):
        return "pc"
    # auto
    p = cfg.get("pc") or {}
    exe = p.get("exe_path") or ""
    try:
        from .pc_client import PcClient
        c = PcClient(cfg)
        if c.find_window():
            log.info("检测到游戏窗口 → 使用 PC 后端")
            return "pc"
    except Exception as e:
        log.debug("PC 窗口探测失败: %s", e)
    if exe:
        log.info("未检测到 PC 窗口，回落 ADB")
    return "adb"


def create_backend(cfg, dry_run: bool = False):
    if dry_run:
        raise RuntimeError("dry-run 由 runner 自己构造 FakeAdb，不走工厂")
    from .adb_client import AdbClient
    if backend_kind(cfg) == "pc":
        from .pc_client import PcClient
        return PcClient(cfg)
    return AdbClient(cfg)
