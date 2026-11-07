"""明日方舟 · 自动公招面板（图形界面 / 打包成 exe 的那一层）。

功能：
  - 一键开始 / 停止（停止是"当前动作做完就退"，不会把游戏点崩）
  - 两种打法：批量挂单（1~4 位全挂满 → 再逐个加急领）/ 标准（一槽到底）
  - 槽位勾选、轮数、轮间隔、模拟运行（不连 ADB，先看流程）
  - 实时日志（按级别着色）+ 本次/累计统计 + 当前动作状态

自检（打包后也能用，不需要控制台）：
    gui.py --selftest      → 模拟跑 1 轮，结果写 logs/selftest.txt
"""
import argparse
import logging
import os
import queue
import sys
import threading
import time

import tkinter as tk
from tkinter import messagebox, ttk

from core import paths
from core.config import load_config
from core.logger import setup_logger
from core.runner import Runner

APP_TITLE = "明日方舟 · 自动公招面板"
COL_BG = "#15161c"
COL_CARD = "#1e2029"
COL_FG = "#e8eaed"
COL_DIM = "#8b93a7"
COL_ACCENT = "#f0a13c"
COL_OK = "#5fcf80"
COL_WARN = "#ffb454"
COL_ERR = "#ff6b6b"

try:                                    # Windows 高 DPI 下别糊
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


class QueueHandler(logging.Handler):
    """把日志塞进 queue，主线程再刷进 Text（tk 不是线程安全的）。"""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put((record.levelno, self.format(record)))
        except Exception:
            pass


class Panel:
    def __init__(self, root: tk.Tk, cfg, log, autostart=False, selftest_path=None):
        self.root = root
        self.cfg = cfg
        self.log = log
        self.runner = None
        self.worker = None
        self.q = queue.Queue()
        self.selftest_path = selftest_path
        self._autostart = autostart
        self._lines = 0

        log.addHandler(QueueHandler(self.q))

        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._pump)
        if not autostart:
            threading.Thread(target=self._detect_device, daemon=True).start()
        if autostart:
            self.root.after(400, self._autostart_run)

    # ------------------------------------------------------------------ 外观
    def _style(self):
        r = self.root
        r.title(APP_TITLE)
        r.geometry("920x660")
        r.minsize(780, 560)
        r.configure(bg=COL_BG)
        icon = os.path.join(paths.ROOT, "assets", "icon.ico")
        if os.path.exists(icon):
            try:
                r.iconbitmap(icon)
            except Exception:
                pass
        st = ttk.Style(r)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=COL_BG, foreground=COL_FG,
                     font=("Microsoft YaHei UI", 10))
        st.configure("Card.TFrame", background=COL_CARD)
        st.configure("TFrame", background=COL_BG)
        st.configure("TLabel", background=COL_BG, foreground=COL_FG)
        st.configure("Card.TLabel", background=COL_CARD, foreground=COL_FG)
        st.configure("Dim.TLabel", background=COL_CARD, foreground=COL_DIM)
        st.configure("Big.TLabel", background=COL_CARD, foreground=COL_ACCENT,
                     font=("Microsoft YaHei UI", 20, "bold"))
        st.configure("Title.TLabel", background=COL_BG, foreground=COL_ACCENT,
                     font=("Microsoft YaHei UI", 13, "bold"))
        st.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        st.configure("Start.TButton", font=("Microsoft YaHei UI", 11, "bold"),
                     padding=(18, 9))
        st.configure("TRadiobutton", background=COL_CARD, foreground=COL_FG)
        st.configure("TCheckbutton", background=COL_CARD, foreground=COL_FG)
        st.configure("TSpinbox", fieldbackground="#2a2d39", foreground=COL_FG,
                     background="#2a2d39")

    # ------------------------------------------------------------------ 布局
    def _build(self):
        pad = {"padx": 14, "pady": 8}

        head = ttk.Frame(self.root, style="TFrame")
        head.pack(fill="x", **pad)
        ttk.Label(head, text="明日方舟 · 自动公招面板", style="Title.TLabel").pack(side="left")
        self.device_lbl = ttk.Label(head, text="设备：未连接", foreground=COL_DIM,
                                    background=COL_BG)
        self.device_lbl.pack(side="right")

        # ---------- 控制卡片 ----------
        card = ttk.Frame(self.root, style="Card.TFrame")
        card.pack(fill="x", padx=14, pady=(0, 8))

        row1 = ttk.Frame(card, style="Card.TFrame")
        row1.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(row1, text="打法", style="Dim.TLabel").pack(side="left", padx=(0, 8))
        self.mode_var = tk.StringVar(value=self.cfg["game"].get("flow_mode", "batch"))
        ttk.Radiobutton(row1, text="批量挂单（1~4 位先全挂满，再逐个加急领）",
                        variable=self.mode_var, value="batch").pack(side="left")
        ttk.Radiobutton(row1, text="标准（一槽到底）",
                        variable=self.mode_var, value="standard").pack(side="left", padx=10)

        row2 = ttk.Frame(card, style="Card.TFrame")
        row2.pack(fill="x", padx=12, pady=4)
        ttk.Label(row2, text="槽位", style="Dim.TLabel").pack(side="left", padx=(0, 8))
        n_slots = int(self.cfg["game"].get("max_slots", 4))
        self.slot_vars = {}
        for i in range(1, n_slots + 1):
            v = tk.BooleanVar(value=True)
            self.slot_vars[i] = v
            ttk.Checkbutton(row2, text=f"{i} 号位", variable=v).pack(side="left", padx=(0, 6))

        ttk.Label(row2, text="轮数", style="Dim.TLabel").pack(side="left", padx=(18, 6))
        self.cycles_var = tk.IntVar(value=int(self.cfg["game"].get("max_cycles", 0) or 0))
        ttk.Spinbox(row2, from_=0, to=99, width=4, textvariable=self.cycles_var).pack(side="left")
        ttk.Label(row2, text="（0=不限）", style="Dim.TLabel").pack(side="left", padx=(4, 14))

        ttk.Label(row2, text="轮间隔", style="Dim.TLabel").pack(side="left", padx=(0, 6))
        self.interval_var = tk.IntVar(value=int(self.cfg["game"].get("loop_interval_min", 5)))
        ttk.Spinbox(row2, from_=0, to=240, width=4,
                    textvariable=self.interval_var).pack(side="left")
        ttk.Label(row2, text="分钟", style="Dim.TLabel").pack(side="left", padx=(4, 0))

        row3 = ttk.Frame(card, style="Card.TFrame")
        row3.pack(fill="x", padx=12, pady=(6, 12))
        self.sim_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text="模拟运行（不连模拟器，只跑流程）",
                        variable=self.sim_var).pack(side="left")
        self.stop_btn = ttk.Button(row3, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right", padx=(8, 0))
        self.start_btn = ttk.Button(row3, text="开始公招", style="Start.TButton",
                                    command=self.start)
        self.start_btn.pack(side="right")

        # ---------- 统计 ----------
        stats = ttk.Frame(self.root, style="Card.TFrame")
        stats.pack(fill="x", padx=14, pady=(0, 8))
        self.sess_lbl = ttk.Label(stats, text="本次：—", style="Card.TLabel")
        self.sess_lbl.pack(side="left", padx=12, pady=10)
        self.total_lbl = ttk.Label(stats, text="累计：—", style="Dim.TLabel")
        self.total_lbl.pack(side="left", padx=12)
        self.best_lbl = ttk.Label(stats, text="最高：—", style="Big.TLabel")
        self.best_lbl.pack(side="right", padx=14, pady=6)

        # ---------- 日志 ----------
        box = ttk.Frame(self.root, style="TFrame")
        box.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        bar = ttk.Frame(box, style="TFrame")
        bar.pack(fill="x")
        ttk.Label(bar, text="运行日志", foreground=COL_DIM, background=COL_BG).pack(side="left")
        ttk.Button(bar, text="打开日志目录", command=self._open_logs).pack(side="right")

        wrap = ttk.Frame(box, style="TFrame")
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.txt = tk.Text(wrap, bg="#101117", fg="#d7dae1", insertbackground=COL_FG,
                           relief="flat", wrap="none", font=("Consolas", 9),
                           padx=8, pady=6, state="disabled")
        sb = ttk.Scrollbar(wrap, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("INFO", foreground="#cfd8dc")
        self.txt.tag_configure("WARNING", foreground=COL_WARN)
        self.txt.tag_configure("ERROR", foreground=COL_ERR)
        self.txt.tag_configure("OK", foreground=COL_OK)

        # ---------- 状态栏 ----------
        self.status_var = tk.StringVar(value="就绪。确认模拟器已开、游戏已装，点「开始公招」。")
        tk.Label(self.root, textvariable=self.status_var, anchor="w",
                 bg="#101117", fg=COL_DIM, padx=14, pady=6,
                 font=("Microsoft YaHei UI", 9)).pack(fill="x", side="bottom")

    # ------------------------------------------------------------------ 动作
    def _detect_device(self):
        """后台探一次 ADB，把设备/分辨率显示出来（不改游戏状态）。"""
        try:
            from core.adb_client import AdbClient
            adb = AdbClient(self.cfg)
            serial = adb.connect()
            try:
                h, w = adb.screencap().shape[:2]
            except Exception:
                w, h = adb.wm_size()
            self._on_state({"phase": "probe", "device": serial, "width": w, "height": h})
        except Exception as e:
            self._on_state({"phase": "probe_fail", "text": str(e)})

    def _open_logs(self):
        paths.ensure_dirs()
        try:
            os.startfile(paths.LOG_DIR)
        except Exception as e:
            self._append(logging.ERROR, f"打不开日志目录: {e}")

    def _append(self, level, line):
        tag = "ERROR" if level >= logging.ERROR else \
              "WARNING" if level >= logging.WARNING else "INFO"
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n", tag)
        self._lines += 1
        if self._lines > 4000:                      # 别让 Text 无限长
            self.txt.delete("1.0", "1200.0")
            self._lines -= 1200
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        slots = [i for i, v in self.slot_vars.items() if v.get()]
        if not slots:
            messagebox.showwarning(APP_TITLE, "至少勾选一个公招位")
            return
        flow = self.mode_var.get()
        cycles = int(self.cycles_var.get() or 0)
        interval = int(self.interval_var.get() or 0)
        dry = bool(self.sim_var.get())

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set(f"运行中 · 模式 {'批量挂单' if flow == 'batch' else '标准'} · "
                            f"槽位 {slots} · {'模拟' if dry else '真机'}")
        self._append(logging.INFO, "─" * 46)

        self.worker = threading.Thread(
            target=self._work, args=(flow, slots, cycles, interval, dry), daemon=True)
        self.worker.start()

    def _work(self, flow, slots, cycles, interval, dry):
        try:
            self.runner = Runner(self.cfg, logger=self.log, dry_run=dry,
                                 on_state=self._on_state)
            self.runner.prepare()
            self.runner.run(flow_mode=flow, slots=slots, cycles=cycles,
                            interval_min=interval)
        except Exception as e:
            import traceback
            self.log.error("运行出错: %s\n%s", e, traceback.format_exc())
            self._on_state({"phase": "error", "text": str(e)})
        finally:
            self.root.after(0, self._idle)

    def stop(self):
        if self.runner:
            self.runner.stop()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("正在停止（等当前动作做完）…")

    def _idle(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("已停止。可再次点「开始公招」。")

    def _on_state(self, st):
        self.root.after(0, lambda: self._apply_state(st))

    def _apply_state(self, st):
        ph = st.get("phase")
        if ph == "probe":
            self.device_lbl.configure(
                text=f"设备：{st.get('device')}  {st.get('width')}x{st.get('height')}（已就绪）")
            self.status_var.set("设备已就绪，点「开始公招」开跑。")
        elif ph == "probe_fail":
            self.device_lbl.configure(text="设备：未连接")
            self.status_var.set(f"没探到模拟器（{st.get('text', '')[:60]}）"
                                "—— 先开 MuMu 并确认游戏已装")
        elif ph == "ready":
            self.device_lbl.configure(
                text=f"设备：{st.get('device')}  {st.get('width')}x{st.get('height')}")
            if st.get("total"):
                self.total_lbl.configure(text=st["total"])
        elif ph in ("place", "claim"):
            cyc = getattr(self.runner, "cycle", 0) if self.runner else 0
            self.status_var.set(f"第 {cyc} 轮 · {st.get('text', '')}")
        elif ph == "sleep":
            self.status_var.set(st.get("text", ""))
        elif ph == "cycle_done":
            self.status_var.set(f"第 {st.get('cycle')} 轮完成，领到 {st.get('done')} 位")
            if st.get("session"):
                self.sess_lbl.configure(text=st["session"])
        elif ph == "stopped":
            if st.get("session"):
                self.sess_lbl.configure(text=st["session"])
            if st.get("total"):
                self.total_lbl.configure(text=st["total"])
            if st.get("best"):
                self.best_lbl.configure(text="最高 " + st["best"])
            self.status_var.set("已停止。")
        elif ph == "error":
            self.status_var.set("出错：" + st.get("text", ""))
        if st.get("record") and st["record"].get("rarity"):
            r = st["record"]
            self.best_lbl.configure(text=f"最新 {r['rarity']}★ {r['name']}")

    # ------------------------------------------------------------------ 循环
    def _pump(self):
        drained = 0
        while drained < 200:
            try:
                level, line = self.q.get_nowait()
            except queue.Empty:
                break
            self._append(level, line)
            drained += 1
            if self.selftest_path and self._lines:
                self._pump_count = getattr(self, "_pump_count", 0) + 1
        if self.worker and not self.worker.is_alive():
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
        self.root.after(120, self._pump)

    # ------------------------------------------------------------------ 自检
    def _autostart_run(self):
        self.sim_var.set(True)
        self.cycles_var.set(1)
        self.interval_var.set(0)
        self.start()
        self._st_deadline = time.time() + 150
        self.root.after(500, self._selftest_poll)

    def _selftest_poll(self):
        if self.worker and not self.worker.is_alive():
            self._selftest_done()
            return
        if time.time() > self._st_deadline:
            self._selftest_done(timeout=True)
            return
        self.root.after(500, self._selftest_poll)

    def _selftest_done(self, timeout=False):
        ok = (not timeout) and self._lines > 5
        try:
            paths.ensure_dirs()
            with open(self.selftest_path, "w", encoding="utf-8") as f:
                f.write(f"{'OK' if ok else 'FAIL'} lines={self._lines} "
                        f"timeout={timeout}\n")
        except Exception:
            pass
        self.root.after(300, self.root.destroy)

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(APP_TITLE, "正在运行，确定停止并退出？"):
                return
            self.stop()
            self.worker.join(timeout=5)
        self.root.destroy()


def run_selftest(cfg, log) -> str:
    root = tk.Tk()
    paths.ensure_dirs()
    path = os.path.join(paths.LOG_DIR, "selftest.txt")
    if os.path.exists(path):
        os.remove(path)
    Panel(root, cfg, log, autostart=True, selftest_path=path)
    root.mainloop()
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "FAIL 没有产出 selftest.txt"


def check_ocr(cfg) -> str:
    """打包后用来自检真 OCR（确认 rapidocr/onnxruntime 的模型被打进 exe 了）。"""
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from core.ocr import OcrEngine

        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 46)
        img = Image.new("RGB", (760, 150), (18, 19, 24))
        ImageDraw.Draw(img).text((18, 46), "公开招募 高级资深干员 09:00",
                                 font=font, fill=(238, 238, 238))
        arr = np.array(img)[:, :, ::-1].copy()          # RGB -> BGR
        texts = OcrEngine(cfg).texts(arr)
        return ("OK " + " | ".join(texts)) if texts else "FAIL 没识别出任何文字"
    except Exception as e:
        return f"FAIL {e}"


def main():
    ap = argparse.ArgumentParser(description=APP_TITLE)
    ap.add_argument("--config", default=None)
    ap.add_argument("--selftest", action="store_true", help="自动模拟跑一轮并退出")
    ap.add_argument("--check-ocr", action="store_true", help="自检 OCR 引擎后退出")
    args = ap.parse_args()

    paths.ensure_dirs()
    cfg = load_config(paths.resolve_config(args.config))
    log = setup_logger(paths.LOG_DIR)

    if args.check_ocr:
        result = check_ocr(cfg)
        paths.ensure_dirs()
        try:
            with open(os.path.join(paths.LOG_DIR, "ocr_check.txt"), "w",
                      encoding="utf-8") as f:
                f.write(result + chr(10))
        except Exception:
            pass
        print(result)
        sys.exit(0 if result.startswith("OK") else 1)

    if args.selftest:
        result = run_selftest(cfg, log)
        print(result)
        sys.exit(0 if result.startswith("OK") else 1)

    root = tk.Tk()
    Panel(root, cfg, log)
    root.mainloop()


if __name__ == "__main__":
    main()
