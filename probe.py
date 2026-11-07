"""真机探针：截图 → OCR → 打印识别到的文字与坐标（不改配置、不点任何东西）。

用途：游戏改版后，关键词对不上时用它看真实界面到底写了什么。

用法：
    python probe.py                 # 截一张，OCR 打印
    python probe.py --save a.png     # 顺便存图
    python probe.py --loop 6 --interval 10   # 每 10 秒一张，共 6 张
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.adb_client import AdbClient
from core.config import load_config
from core.ocr import OcrEngine


def one(adb, ocr, cfg, path=None):
    img = adb.screencap()
    h, w = img.shape[:2]
    print(f"\n--- 截图 {w}x{h}")
    if path:
        import cv2
        cv2.imwrite(path, img)
        print(f"    已存 {path}")
    items = ocr.recognize(img)
    if not items:
        print("    OCR 无文字")
    for t in items:
        cx, cy = t.center
        print(f"    ({cx:6.0f},{cy:6.0f}) {t.score:.2f}  {t.text}")
    return img


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--save", default="")
    ap.add_argument("--loop", type=int, default=1)
    ap.add_argument("--interval", type=float, default=10)
    a = ap.parse_args()

    cfg = load_config(a.config)
    adb = AdbClient(cfg)
    print("设备:", adb.connect(), "| 焦点:", adb.current_package())
    ocr = OcrEngine(cfg)

    for i in range(a.loop):
        path = a.save or (f"logs/probe_{time.strftime('%H%M%S')}.png" if a.loop > 1 else "")
        if path and i and a.save:
            path = a.save.replace(".png", f"_{i}.png")
        one(adb, ocr, cfg, path)
        if i < a.loop - 1:
            time.sleep(a.interval)


if __name__ == "__main__":
    main()
