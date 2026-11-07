"""明日方舟 自动公招 · 命令行入口（GUI 面板见 gui.py）。

用法：
    python main.py                    # 默认：批量挂单（1~4 位全挂满 → 再逐个加急领）
    python main.py --mode standard    # 标准：一槽到底（挂上 → 立刻加急领）
    python main.py --once             # 只跑一轮
    python main.py --cycles 3         # 最多 3 轮
    python main.py --slots 2          # 只处理前 2 个公招位
    python main.py --interval 5       # 轮间隔分钟
    python main.py --dry-run          # 离线模拟（不碰 ADB、不发邮件）

流程（每轮）：
    确保游戏前台 → 进公开招募 → [批量] 1~4 位依次挂 9h → 再逐个加急领取 → 记账/推送
"""
import argparse
import signal
import sys

from core import paths
from core.config import load_config
from core.logger import setup_logger
from core.runner import Runner


def main():
    ap = argparse.ArgumentParser(description="明日方舟自动公招")
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true", help="离线模拟，不连 ADB")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--cycles", type=int, default=0, help="最多几轮（0=按配置）")
    ap.add_argument("--slots", type=int, default=0, help="本轮处理几个公招位（0=按配置）")
    ap.add_argument("--mode", default=None, choices=["batch", "standard"],
                    help="batch=先全挂满再逐个领（默认）；standard=一槽到底")
    ap.add_argument("--interval", type=float, default=None, help="轮间隔分钟")
    args = ap.parse_args()

    paths.ensure_dirs()
    cfg = load_config(paths.resolve_config(args.config))
    log = setup_logger(paths.LOG_DIR)

    flow_mode = args.mode or cfg["game"].get("flow_mode", "batch")
    cycles = 1 if args.once else args.cycles or int(cfg["game"].get("max_cycles", 0) or 0)
    if args.once:
        cycles = 1
    max_slots = args.slots or int(cfg["game"].get("max_slots", 4))
    slots = list(range(1, max_slots + 1))
    interval = args.interval if args.interval is not None else \
        float(cfg["game"].get("loop_interval_min", 5))

    log.info("=" * 52)
    log.info("明日方舟自动公招 启动%s | 模式 %s | 槽位 %s | %s 轮",
             "（离线模拟）" if args.dry_run else "", flow_mode, slots,
             cycles or "不限")

    runner = Runner(cfg, logger=log, dry_run=args.dry_run)

    def _on_signal(signum, frame):
        runner.stop()

    signal.signal(signal.SIGINT, _on_signal)

    try:
        runner.prepare()
    except Exception as e:
        log.error("初始化失败: %s", e)
        sys.exit(1)

    try:
        runner.run(flow_mode=flow_mode, slots=slots, cycles=cycles,
                   interval_min=interval)
    except KeyboardInterrupt:
        runner.stop()
    log.info("脚本结束")


if __name__ == "__main__":
    main()
