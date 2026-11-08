"""守候式只读验证：等玩家离开战斗界面后，自动跑一遍 `--flow-check`（不提交、不消耗）。

用法：
    python pc_wait_flow.py [超时分钟数，默认 12]

为什么要有这个：
    PC 端第一次迁移时，最怕「战斗中点坐标 → 误部署 / 误消耗」。
    这里先由 OCR 判定当前界面，只有明确是大厅（看得到「公开招募」）才动手；
    战斗中一律等待，绝不点任何坐标。
"""
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.config import load_config
from core.device import create_backend
from core.ocr import OcrEngine

BATTLE = ["剩余可放置角色", "ENEMY", "行动结束", "理智", "自动部署", "撤退"]
LOBBY = ["公开招募", "RECRUIT", "RECRUITMENT"]


def main():
    limit = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    cfg = load_config("config.yaml")
    dev = create_backend(cfg)
    dev.connect()
    dev.capture = "screen"
    ocr = OcrEngine(cfg)
    end = time.time() + limit * 60
    last = ""
    while time.time() < end:
        try:
            dev._focus()
            time.sleep(0.4)
            texts = ocr.texts(dev.screencap())
        except Exception as e:
            print("抓帧失败:", e)
            time.sleep(5)
            continue
        if any(any(k in t for k in LOBBY) for t in texts):
            print("\n检测到大厅 → 开始只读验证")
            from pc_probe import cmd_flow_check
            return cmd_flow_check(cfg)
        state = "战斗/结算" if any(any(k in t for k in BATTLE) for t in texts) else "其他界面"
        if state != last:
            print(f"[{time.strftime('%H:%M:%S')}] 当前 {state}，不动坐标，继续等…")
            last = state
        time.sleep(6)
    print(f"\n等了 {limit:.0f} 分钟还没回到大厅 → 先停。等你回到主界面再跑："
          f"python pc_probe.py --flow-check")


if __name__ == "__main__":
    main()
