"""UI 坐标校准工具（Tkinter 版，无需 opencv GUI）。

在装有 MuMu 的机器上运行：
    python calibrate.py

流程：连接模拟器 → 截取当前屏幕 → 弹出窗口 → 依次点击关键点 → 输出坐标。
"""
import sys
import tkinter as tk

import cv2
from PIL import Image, ImageTk

from core.adb_client import AdbClient, AdbError
from core.config import load_config

POINTS = [
    ("slot_1", "第一个公招位中心"),
    ("slot_2", "第二个公招位中心"),
    ("slot_3", "第三个公招位中心"),
    ("slot_4", "第四个公招位中心"),
]


def main():
    cfg = load_config("config.yaml")
    adb = AdbClient(cfg)
    try:
        adb.connect()
    except AdbError as e:
        print(f"连接失败: {e}")
        sys.exit(1)

    w, h = adb.wm_size()
    print(f"分辨率: {w}x{h}")

    img_bgr = adb.screencap()
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

    root = tk.Tk()
    root.title("公招坐标校准")

    scale = min(1.0, 900 / max(w, h))
    disp_w, disp_h = int(w * scale), int(h * scale)
    pil = Image.fromarray(img_rgb).resize((disp_w, disp_h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(pil)
    canvas = tk.Canvas(root, width=disp_w, height=disp_h)
    canvas.pack()
    canvas.create_image(0, 0, anchor=tk.NW, image=photo)

    info = tk.Label(root, text=f"点击：{POINTS[0][1]} ({POINTS[0][0]})", font=("", 14))
    info.pack(pady=6)

    clicks = []
    idx = [0]

    def on_click(event):
        ox, oy = int(event.x / scale), int(event.y / scale)
        px, py = ox / w, oy / h
        clicks.append((ox, oy, px, py))
        print(f"  已记录 ({ox},{oy}) 比例 ({px:.4f},{py:.4f})")
        canvas.create_oval(event.x - 5, event.y - 5, event.x + 5, event.y + 5,
                           outline="red", width=2)
        idx[0] += 1
        if idx[0] < len(POINTS):
            info.config(text=f"点击：{POINTS[idx[0]][1]} ({POINTS[idx[0]][0]})")
        else:
            info.config(text="全部采集完成，关闭窗口查看结果")

    canvas.bind("<Button-1>", on_click)
    print("\n请在窗口中依次点击 4 个公招位中心。")
    root.mainloop()

    print("\n" + "=" * 40)
    print("请把下面内容填进 config.yaml 的 ui.slots:")
    print("ui:")
    print("  slots:")
    for i, (name, _) in enumerate(POINTS):
        if i < len(clicks):
            _, _, px, py = clicks[i]
            print(f'    - {{x: {px:.4f}, y: {py:.4f}}}   # {name}')
        else:
            print(f'    - {{x: 0.5000, y: 0.3000}}   # {name} (未采集)')


if __name__ == "__main__":
    main()
