"""PC 输入链路自检：开一个可控的 Tk 窗口，按已知坐标点它，验证「坐标映射 + 注入方式」。

不碰游戏、不消耗任何资源，用来回答三个问题：
  1) 客户区坐标 → 屏幕坐标的换算对不对（DPI / 窗口边框 / 多显示器）；
  2) sendinput 和 postmessage 两种注入方式，哪种在当前环境真的能落点；
  3) 抓屏尺寸、wm_size 和实际客户区是否一致。

用法：
    python pc_selftest.py
"""
import ctypes
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, ".")

from core.config import load_config
from core.pc_client import PcClient, PcError

TARGETS = [
    ("A", 120, 120),
    ("B", 880, 120),
    ("C", 120, 480),
    ("D", 880, 480),
]
BTN_W, BTN_H = 200, 120


def run(mode):
    import tkinter as tk

    root = tk.Tk()
    root.title(f"pc_selftest - {mode}")
    root.geometry("1200x720+80+80")
    hits = []

    for name, x, y in TARGETS:
        b = tk.Button(root, text=name, font=("Consolas", 20),
                      command=lambda n=name: hits.append(n))
        b.place(x=x, y=y, width=BTN_W, height=BTN_H)

    def pump(sec):
        end = time.time() + sec
        while time.time() < end:
            root.update()
            time.sleep(0.01)

    pump(0.6)
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
    cfg = load_config("config.yaml")
    c = PcClient(cfg)
    c._bind(hwnd)
    c.input_mode = mode
    w, h = c.wm_size()
    img = c.screencap()

    print(f"\n=== {mode} ===")
    print(f"窗口 hwnd={hwnd}  wm_size={w}x{h}  截图={img.shape[1]}x{img.shape[0]}")

    if mode == "sendinput":
        ok = c._focus()
        print(f"抢焦点: {'成功' if ok else '失败（会被前台锁挡住）'}")
        pump(0.3)

    results = []
    for name, x, y in TARGETS:
        cx, cy = x + BTN_W // 2, y + BTN_H // 2
        c.tap(cx, cy)
        pump(0.35)
        results.append((name, cx, cy, name in hits))

    for name, cx, cy, ok in results:
        print(f"  tap({cx:>4},{cy:>3}) 按钮 {name} -> {'[OK] 命中' if ok else '[--] 没反应'}")
    acc = sum(1 for _, _, _, ok in results if ok)
    print(f"命中 {acc}/{len(TARGETS)}")
    root.destroy()
    return acc


def main():
    cfg = load_config("config.yaml")
    print("=" * 56)
    print("PC 输入链路自检（不碰游戏）")
    print("=" * 56)
    scores = {}
    for mode in ("postmessage", "sendinput"):
        try:
            scores[mode] = run(mode)
        except PcError as e:
            print(f"{mode} 失败: {e}")
            scores[mode] = 0
    print("\n" + "=" * 56)
    for m, s in scores.items():
        print(f"{m:<12} {s}/4")
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        print("两种都点不动 -> 当前进程被限制注入输入（管理员/正常终端跑，或换虚拟 HID）")
    else:
        print(f"建议 config.yaml → pc.input: \"{best}\"")


if __name__ == "__main__":
    main()
