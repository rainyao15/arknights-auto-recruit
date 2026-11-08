"""明日方舟 · 自动公招面板（可视化控制台）。

布局：
  ┌──────────┬───────────────────────────────────────────┐
  │ 控制台    │  KPI：本次 / 4★+ / 最高星 / 折算票据        │
  │  ● 开关   │  星级分布            4★+ 干员榜（重点）     │
  │  打法     │  最近出货记录（4★+ 高亮）                   │
  │  槽位     ├───────────────────────────────────────────┤
  │  轮数     │  运行日志（级别过滤 / 暂停滚动 / 导出）      │
  └──────────┴───────────────────────────────────────────┘

自检（打包后也能用，不需要控制台）：
    python gui.py --selftest      → 模拟跑 1 轮，结果写 logs/selftest.txt
    python gui.py --check-ocr     → 验证 OCR 引擎可用
"""
import argparse
import json
import logging
import os
import queue
import sys
import threading
import time

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from core import paths
from core.config import load_config
from core.logger import setup_logger
from core.runner import Runner

APP_TITLE = "明日方舟 · 自动公招面板"

# ---- 配色 ----
COL_BG = "#13141a"
COL_SIDE = "#191b23"
COL_CARD = "#1e2029"
COL_CARD2 = "#242734"
COL_FG = "#e8eaed"
COL_DIM = "#8b93a7"
COL_ACCENT = "#f0a13c"
COL_OK = "#5fcf80"
COL_WARN = "#ffb454"
COL_ERR = "#ff6b6b"
COL_LINE = "#2c2f3d"

# ---- 星级配色：4★ 起是"重点"，越往上越暖越亮 ----
STAR_COLOR = {
    1: "#4a4f5e", 2: "#5a6070", 3: "#8b93a7",
    4: "#5fb0ff", 5: "#f0a13c", 6: "#ffd166",
}
STAR_NAME = {1: "1★", 2: "2★", 3: "3★", 4: "4★", 5: "5★", 6: "6★"}

try:                                    # Windows 高 DPI 下别糊
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


# ====================================================================== 组件
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


class Toggle(tk.Canvas):
    """开关控件：点一下 = 开始/停止。"""

    W, H = 62, 30

    def __init__(self, master, command=None, bg=COL_CARD):
        super().__init__(master, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.command = command
        self.on = False
        self.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h, r = self.W, self.H, self.H / 2 - 1
        track = COL_OK if self.on else "#3a3f4d"
        # 圆角轨道 = 两个圆 + 中间矩形
        self.create_oval(1, 1, h - 1, h - 1, fill=track, outline=track)
        self.create_oval(w - h + 1, 1, w - 1, h - 1, fill=track, outline=track)
        self.create_rectangle(h / 2, 1, w - h / 2, h - 1, fill=track, outline=track)
        kx = w - r - 3 if self.on else r + 3
        self.create_oval(kx - r + 4, 4, kx + r - 4, h - 4, fill="#ffffff", outline="")

    def set(self, on: bool):
        if on != self.on:
            self.on = on
            self._draw()

    def _click(self, _evt=None):
        if self.command:
            self.command()          # 由面板决定真正状态（启动成功才置 on）


class Kpi(tk.Frame):
    """一张数字卡片。"""

    def __init__(self, master, title, value="—", sub="", color=COL_FG):
        super().__init__(master, bg=COL_CARD)
        self.v_title = tk.Label(self, text=title, bg=COL_CARD, fg=COL_DIM,
                                font=("Microsoft YaHei UI", 9))
        self.v_title.pack(anchor="w", padx=12, pady=(9, 0))
        self.v_value = tk.Label(self, text=value, bg=COL_CARD, fg=color,
                                font=("Microsoft YaHei UI", 22, "bold"))
        self.v_value.pack(anchor="w", padx=12)
        self.v_sub = tk.Label(self, text=sub, bg=COL_CARD, fg=COL_DIM,
                              font=("Microsoft YaHei UI", 8))
        self.v_sub.pack(anchor="w", padx=12, pady=(0, 9))

    def set(self, value=None, sub=None, color=None):
        if value is not None:
            self.v_value.configure(text=value)
        if sub is not None:
            self.v_sub.configure(text=sub)
        if color is not None:
            self.v_value.configure(fg=color)


# ====================================================================== 面板
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
        self._running = False
        self._elev_blocked = False
        self._t0 = None
        self._deadline = None
        self._pause_scroll = tk.BooleanVar(value=False)
        self._level_var = tk.StringVar(value="全部")
        self._focus_4 = tk.BooleanVar(value=False)      # 只看 4★+
        self._scope = "session"                         # 4★+ 榜：session | total

        # ---- 数据 ----
        self.sess = []                    # 本次出货记录
        self.total_counts = {}            # 累计星级 → 次数
        self.opp_sess = {}                # (rarity, name) → 次数（本次）
        self.opp_total = {}               # (rarity, name) → 次数（累计，4★+）
        self.sim = False

        log.addHandler(QueueHandler(self.q))

        self._style()
        self._build()
        self._load_total()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(120, self._pump)
        self.root.after(700, self._tick)
        if autostart:
            self.root.after(400, self._autostart_run)
        else:
            threading.Thread(target=self._detect_device, daemon=True).start()

    # ------------------------------------------------------------------ 外观
    def _style(self):
        r = self.root
        r.title(APP_TITLE)
        r.geometry("1180x760")
        r.minsize(980, 620)
        r.configure(bg=COL_BG)
        # 窗口图标：源码跑用项目里的，打包后从 _internal 里取（exe 旁边没 assets 也能显示）
        icon = os.path.join(paths.ROOT, "assets", "icon.ico")
        if not os.path.exists(icon):
            base = getattr(sys, "_MEIPASS", "")
            if base:
                icon = os.path.join(base, "assets", "icon.ico")
        if os.path.exists(icon):
            try:
                r.iconbitmap(icon)
                r.after(200, lambda: self._keep_icon(icon))
            except Exception:
                pass

        st = ttk.Style(r)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=COL_BG, foreground=COL_FG,
                     font=("Microsoft YaHei UI", 10))
        st.configure("TFrame", background=COL_BG)
        st.configure("Side.TFrame", background=COL_SIDE)
        st.configure("Card.TFrame", background=COL_CARD)
        st.configure("TLabel", background=COL_BG, foreground=COL_FG)
        st.configure("Card.TLabel", background=COL_CARD, foreground=COL_FG)
        st.configure("Dim.TLabel", background=COL_SIDE, foreground=COL_DIM)
        st.configure("DimCard.TLabel", background=COL_CARD, foreground=COL_DIM)
        st.configure("Title.TLabel", background=COL_BG, foreground=COL_ACCENT,
                     font=("Microsoft YaHei UI", 14, "bold"))
        st.configure("H.TLabel", background=COL_BG, foreground=COL_FG,
                     font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("TCheckbutton", background=COL_SIDE, foreground=COL_FG)
        st.configure("Card.TCheckbutton", background=COL_CARD, foreground=COL_FG)
        st.configure("TRadiobutton", background=COL_SIDE, foreground=COL_FG)
        st.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(8, 6))
        st.configure("Treeview", background="#101117", fieldbackground="#101117",
                     foreground="#d7dae1", rowheight=22, borderwidth=0)
        st.configure("Treeview.Heading", background="#242734", foreground="#cfd8dc",
                     relief="flat", font=("Microsoft YaHei UI", 9))
        st.map("Treeview", background=[("selected", "#2f3548")])
        st.configure("TProgressbar", background=COL_ACCENT, troughcolor="#2a2d39",
                     bordercolor="#2a2d39", lightcolor=COL_ACCENT,
                     darkcolor=COL_ACCENT)
        st.configure("TCombobox", fieldbackground="#2a2d39", background="#2a2d39",
                     foreground=COL_FG)
        # 暗色滚动条（clam 默认是浅灰块，很扎眼）
        st.configure("Vertical.TScrollbar", background="#2a2d39", troughcolor="#15161c",
                     bordercolor="#15161c", arrowcolor=COL_DIM, relief="flat")
        st.map("Vertical.TScrollbar", background=[("active", "#3a3f4d")])
        st.configure("TSpinbox", fieldbackground="#2a2d39", foreground=COL_FG,
                     background="#2a2d39", arrowcolor=COL_DIM, borderwidth=0,
                     insertcolor=COL_FG)
        st.map("TCombobox",
               fieldbackground=[("readonly", "#2a2d39")],
               foreground=[("readonly", COL_FG)],
               selectbackground=[("readonly", "#2a2d39")],
               selectforeground=[("readonly", COL_FG)])

    def _keep_icon(self, path):
        """Windows 上换个 DPI/还原窗口后任务栏图标会掉，补设一次。"""
        try:
            self.root.iconbitmap(path)
            self.root.iconbitmap(default=path)
        except Exception:
            pass

    # ------------------------------------------------------------------ 布局
    def _build(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self._build_sidebar()
        right = tk.PanedWindow(self.root, orient="vertical", bg=COL_BG, bd=0,
                               sashwidth=6, sashrelief="flat")
        right.grid(row=0, column=1, sticky="nsew", padx=(0, 10), pady=10)

        stats = ttk.Frame(right, style="TFrame")
        logs = ttk.Frame(right, style="TFrame")
        self._build_stats(stats)
        self._build_logs(logs)
        right.add(stats, stretch="always", minsize=340)
        right.add(logs, stretch="always", minsize=170)
        self._paned = right
        self.root.after(80, lambda: right.sash_place(0, 0, 470))

        # 状态栏
        self.status_var = tk.StringVar(value="就绪 · 确认游戏已启动，点右侧开关开始")
        bar = tk.Frame(self.root, bg="#0e0f14")
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.status_lbl = tk.Label(bar, textvariable=self.status_var, anchor="w",
                                   bg="#0e0f14", fg=COL_DIM, padx=14, pady=6,
                                   font=("Microsoft YaHei UI", 9))
        self.status_lbl.pack(side="left")
        self.uptime_lbl = tk.Label(bar, text="--:--:--", anchor="e", bg="#0e0f14",
                                   fg=COL_DIM, padx=14, font=("Consolas", 9))
        self.uptime_lbl.pack(side="right")

    # ---------------------------------------------------------- 左：控制台
    def _build_sidebar(self):
        side = ttk.Frame(self.root, style="Side.TFrame", width=272)
        side.grid(row=0, column=0, sticky="nsw", padx=(10, 8), pady=10)
        side.grid_propagate(False)

        tk.Label(side, text="自动招募", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16, pady=(14, 2))
        sw = tk.Frame(side, bg=COL_SIDE)
        sw.pack(anchor="w", padx=16, fill="x")
        self.toggle = Toggle(sw, command=self._toggle_click, bg=COL_SIDE)
        self.toggle.pack(side="left")
        self.toggle_lbl = tk.Label(sw, text="已停止", bg=COL_SIDE, fg=COL_DIM,
                                   font=("Microsoft YaHei UI", 12, "bold"))
        self.toggle_lbl.pack(side="left", padx=12)

        ttk.Separator(side).pack(fill="x", padx=14, pady=10)

        # 打法
        tk.Label(side, text="打法", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16)
        self.mode_var = tk.StringVar(value=self.cfg["game"].get("flow_mode", "batch"))
        ttk.Radiobutton(side, text="批量挂单 · 省 OCR", variable=self.mode_var,
                        value="batch").pack(anchor="w", padx=16, pady=2)
        ttk.Radiobutton(side, text="标准 · 一槽到底", variable=self.mode_var,
                        value="standard").pack(anchor="w", padx=16)

        tk.Frame(side, bg=COL_SIDE, height=8).pack()

        # 槽位
        tk.Label(side, text="公招位", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16)
        slot_row = tk.Frame(side, bg=COL_SIDE)
        slot_row.pack(anchor="w", padx=12, pady=2)
        n_slots = int(self.cfg["game"].get("max_slots", 4))
        self.slot_vars = {}
        for i in range(1, n_slots + 1):
            v = tk.BooleanVar(value=True)
            self.slot_vars[i] = v
            ttk.Checkbutton(slot_row, text=str(i), variable=v).pack(side="left")

        tk.Frame(side, bg=COL_SIDE, height=8).pack()

        # 轮数 / 间隔
        num = tk.Frame(side, bg=COL_SIDE)
        num.pack(fill="x", padx=16)
        tk.Label(num, text="轮数", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 9)).grid(row=0, column=0, sticky="w")
        self.cycles_var = tk.IntVar(value=int(self.cfg["game"].get("max_cycles", 0) or 0))
        ttk.Spinbox(num, from_=0, to=999, width=5,
                    textvariable=self.cycles_var).grid(row=0, column=1, padx=6, sticky="w")
        tk.Label(num, text="0=一直跑", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 8)).grid(row=0, column=2, sticky="w")

        tk.Label(num, text="轮间隔", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 9)).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.interval_var = tk.IntVar(value=int(self.cfg["game"].get("loop_interval_min", 5)))
        ttk.Spinbox(num, from_=0, to=600, width=5,
                    textvariable=self.interval_var).grid(row=1, column=1, padx=6, pady=(8, 0), sticky="w")
        tk.Label(num, text="分钟", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 8)).grid(row=1, column=2, pady=(8, 0), sticky="w")

        self.sim_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(side, text="模拟运行（不碰游戏）", variable=self.sim_var).pack(
            anchor="w", padx=16, pady=(10, 0))
        # 屏幕抓取只能拍最前面的窗口：面板盖在游戏上就会把面板自己拍进去
        self.minimize_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(side, text="运行时最小化面板", variable=self.minimize_var).pack(
            anchor="w", padx=16, pady=(2, 0))

        ttk.Separator(side).pack(fill="x", padx=14, pady=10)

        # 进度
        tk.Label(side, text="当前进度", bg=COL_SIDE, fg=COL_DIM,
                 font=("Microsoft YaHei UI", 9)).pack(anchor="w", padx=16)
        self.prog_lbl = tk.Label(side, text="第 0 轮", bg=COL_SIDE, fg=COL_FG,
                                 font=("Microsoft YaHei UI", 11, "bold"))
        self.prog_lbl.pack(anchor="w", padx=16, pady=(2, 6))
        self.prog = ttk.Progressbar(side, mode="determinate", maximum=100)
        self.prog.pack(fill="x", padx=16)
        self.phase_lbl = tk.Label(side, text="—", bg=COL_SIDE, fg=COL_DIM,
                                  wraplength=236, justify="left",
                                  font=("Microsoft YaHei UI", 9))
        self.phase_lbl.pack(anchor="w", padx=16, pady=(8, 0))

        tk.Frame(side, bg=COL_SIDE).pack(expand=True, fill="both")

        self.device_lbl = tk.Label(side, text="设备：未连接", bg=COL_SIDE, fg=COL_DIM,
                                   wraplength=236, justify="left",
                                   font=("Microsoft YaHei UI", 8))
        self.device_lbl.pack(anchor="w", padx=16, pady=(0, 4))
        self._build_perm_row(side)

    def _build_perm_row(self, side):
        """权限行：普通用户运行时给一个「以管理员重启」补丁按钮。"""
        from core.elevation import is_admin
        self._is_admin = is_admin()
        row = tk.Frame(side, bg=COL_SIDE)
        row.pack(anchor="w", fill="x", padx=16, pady=(0, 10))
        if self._is_admin:
            tk.Label(row, text="权限：管理员", bg=COL_SIDE, fg=COL_OK,
                     font=("Microsoft YaHei UI", 8)).pack(side="left")
            return
        tk.Label(row, text="权限：普通用户", bg=COL_SIDE, fg=COL_WARN,
                 font=("Microsoft YaHei UI", 8)).pack(side="left")
        self.elev_btn = tk.Label(row, text="以管理员重启", bg=COL_CARD2, fg=COL_ACCENT,
                                 padx=8, pady=2, cursor="hand2",
                                 font=("Microsoft YaHei UI", 8))
        self.elev_btn.pack(side="right")
        self.elev_btn.bind("<Button-1>", lambda e: self._elevate())

    def _elevate(self):
        from core.elevation import relaunch_as_admin
        if relaunch_as_admin():
            self.root.destroy()
        else:
            messagebox.showinfo(APP_TITLE, "提权被取消。点「开始」时会再问一次。")

    # ---------------------------------------------------------- 右上：统计
    def _build_stats(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # ---- KPI 行 ----
        kpi = tk.Frame(parent, bg=COL_BG)
        kpi.grid(row=0, column=0, sticky="ew")
        for i in range(4):
            kpi.columnconfigure(i, weight=1)
        self.k_total = Kpi(kpi, "本次出货", "0", "次")
        self.k_high = Kpi(kpi, "4★ 及以上", "0", "占 0%", COL_ACCENT)
        self.k_best = Kpi(kpi, "最高星", "—", "还没出货", STAR_COLOR[6])
        self.k_ticket = Kpi(kpi, "折算票据", "0", "按 config 权重")
        for i, k in enumerate((self.k_total, self.k_high, self.k_best, self.k_ticket)):
            k.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))

        # ---- 分布 + 4★+ 榜 ----
        mid = tk.Frame(parent, bg=COL_BG)
        mid.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        mid.columnconfigure(0, weight=3)
        mid.columnconfigure(1, weight=2)

        dist = tk.Frame(mid, bg=COL_CARD)
        dist.grid(row=0, column=0, sticky="nsew")
        cap = tk.Frame(dist, bg=COL_CARD)
        cap.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(cap, text="星级分布 · 本次", bg=COL_CARD, fg=COL_FG,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.dist_hint = tk.Label(cap, text="累计：—", bg=COL_CARD, fg=COL_DIM,
                                  font=("Microsoft YaHei UI", 8))
        self.dist_hint.pack(side="right")
        self.dist_canvas = tk.Canvas(dist, bg=COL_CARD, height=132,
                                     highlightthickness=0)
        self.dist_canvas.pack(fill="x", padx=12, pady=(2, 8))
        self.dist_canvas.bind("<Configure>", lambda e: self._draw_dist())

        top = tk.Frame(mid, bg=COL_CARD)
        top.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        cap2 = tk.Frame(top, bg=COL_CARD)
        cap2.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(cap2, text="4★+ 干员榜 · 本次", bg=COL_CARD, fg=COL_ACCENT,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.scope_btn = tk.Label(cap2, text="累计", bg=COL_CARD2, fg=COL_DIM,
                                  padx=8, pady=2, cursor="hand2",
                                  font=("Microsoft YaHei UI", 8))
        self.scope_btn.pack(side="right")
        self.scope_btn.bind("<Button-1>", lambda e: self._toggle_scope())
        wrap = tk.Frame(top, bg=COL_CARD)
        wrap.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        self.top_txt = tk.Text(wrap, bg="#101117", fg=COL_FG, relief="flat",
                               font=("Microsoft YaHei UI", 10), height=5,
                               wrap="none", padx=8, pady=6, state="disabled",
                               insertbackground=COL_FG)
        sb = ttk.Scrollbar(wrap, command=self.top_txt.yview)
        self.top_txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.top_txt.pack(side="left", fill="both", expand=True)
        for r, c in STAR_COLOR.items():
            self.top_txt.tag_configure(f"s{r}", foreground=c)
        self.top_txt.tag_configure("dim", foreground=COL_DIM)

        # ---- 最近记录 ----
        table = tk.Frame(parent, bg=COL_BG)
        table.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        head = tk.Frame(table, bg=COL_BG)
        head.pack(fill="x")
        tk.Label(head, text="最近出货", bg=COL_BG, fg=COL_FG,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        ttk.Checkbutton(head, text="只看 4★+", variable=self._focus_4,
                        command=self._redraw_table,
                        style="TCheckbutton").pack(side="right")

        cols = ("time", "slot", "star", "name", "tags")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", height=5)
        for col, txt, w, anchor in (
                ("time", "时间", 84, "w"), ("slot", "位", 40, "center"),
                ("star", "星级", 56, "center"), ("name", "干员", 116, "w"),
                ("tags", "命中词条", 380, "w")):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w, anchor=anchor,
                             stretch=(col == "tags"))
        self.tree.tag_configure("r6", foreground=STAR_COLOR[6])
        self.tree.tag_configure("r5", foreground=STAR_COLOR[5])
        self.tree.tag_configure("r4", foreground=STAR_COLOR[4])
        self.tree.tag_configure("r3", foreground="#9aa3b2")
        sb2 = ttk.Scrollbar(table, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)

    # ---------------------------------------------------------- 右下：日志
    def _build_logs(self, parent):
        bar = tk.Frame(parent, bg=COL_BG)
        bar.pack(fill="x", pady=(6, 0))
        tk.Label(bar, text="运行日志", bg=COL_BG, fg=COL_FG,
                 font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")

        ttk.Button(bar, text="打开目录", command=self._open_logs).pack(side="right")
        ttk.Button(bar, text="导出", command=self._export_log).pack(side="right", padx=6)
        ttk.Checkbutton(bar, text="暂停滚动", variable=self._pause_scroll,
                        style="TCheckbutton").pack(side="right", padx=6)
        ttk.Combobox(bar, textvariable=self._level_var, width=7, state="readonly",
                     values=("全部", "常规", "警告+", "仅错误")).pack(side="right", padx=6)

        wrap = tk.Frame(parent, bg=COL_BG)
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.txt = tk.Text(wrap, bg="#0c0d12", fg="#d7dae1", relief="flat",
                           wrap="none", font=("Consolas", 9), padx=8, pady=6,
                           state="disabled", insertbackground=COL_FG)
        sb = ttk.Scrollbar(wrap, command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.txt.pack(side="left", fill="both", expand=True)
        self.txt.tag_configure("INFO", foreground="#c6ccd6")
        self.txt.tag_configure("OK", foreground=COL_OK)
        self.txt.tag_configure("WARNING", foreground=COL_WARN)
        self.txt.tag_configure("ERROR", foreground=COL_ERR)
        self.txt.tag_configure("HIGH", foreground=COL_ACCENT)

    # ------------------------------------------------------------------ 设备
    def _detect_device(self):
        try:
            from core.device import create_backend
            dev = create_backend(self.cfg)
            serial = dev.connect()
            try:
                h, w = dev.screencap().shape[:2]
            except Exception:
                w, h = dev.wm_size()
            self._on_state({"phase": "probe", "device": serial, "width": w, "height": h})
        except Exception as e:
            self._on_state({"phase": "probe_fail", "text": str(e)})

    # ------------------------------------------------------------------ 动作
    def _toggle_click(self):
        if self._running:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        slots = [i for i, v in self.slot_vars.items() if v.get()]
        if not slots:
            messagebox.showwarning(APP_TITLE, "至少勾选一个公招位")
            return
        self.sim = bool(self.sim_var.get())
        if not self.sim and not self._is_admin:
            self._maybe_elevate()
            if self._elev_blocked:
                return
        cycles = int(self.cycles_var.get() or 0)
        interval = int(self.interval_var.get() or 0)
        flow = self.mode_var.get()

        self._running = True
        self._t0 = time.time()
        self._deadline = None
        self.toggle.set(True)
        self.toggle_lbl.configure(text="运行中", fg=COL_OK)
        self.prog.configure(value=0)
        self.status_var.set(f"运行中 · {'批量挂单' if flow == 'batch' else '标准'} · "
                            f"槽位 {slots} · {'不限轮数' if not cycles else str(cycles) + ' 轮'}"
                            f"{' · 模拟' if self.sim else ''}")

        if self.minimize_var.get():
            try:
                self.root.iconify()          # 别挡住游戏，否则截图拍到的是面板自己
            except Exception:
                pass

        self.worker = threading.Thread(
            target=self._work, args=(flow, slots, cycles, interval, self.sim),
            daemon=True)
        self.worker.start()

    def _maybe_elevate(self):
        """PC 端非管理员开跑 → 直接问一次，别等到「点了没反应」才纳闷。"""
        from core.elevation import needs_elevation, relaunch_as_admin
        self._elev_blocked = False
        try:
            need = needs_elevation(self.cfg)
        except Exception:
            need = False
        if not need:
            return
        if messagebox.askyesno(
                APP_TITLE,
                "\n".join([
                    "检测到明日方舟 PC 客户端是管理员启动的。",
                    "普通权限下脚本抢不到焦点、点不动游戏。",
                    "",
                    "现在用管理员身份重启面板？",
                ])):
            if relaunch_as_admin():
                self.root.destroy()
            else:
                self._elev_blocked = True

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
        self.status_var.set("正在停止（等当前这一步做完）…")
        self.toggle_lbl.configure(text="停止中", fg=COL_WARN)

    def _idle(self):
        self._running = False
        try:
            self.root.deiconify()
        except Exception:
            pass
        self.toggle.set(False)
        self.toggle_lbl.configure(text="已停止", fg=COL_DIM)
        self.status_var.set("已停止 · 可再次打开开关")
        self._load_total()
        self._refresh()

    # ------------------------------------------------------------------ 状态
    def _on_state(self, st):
        self.root.after(0, lambda: self._apply_state(st))

    def _apply_state(self, st):
        ph = st.get("phase")
        if ph == "probe":
            self.device_lbl.configure(
                text=f"设备：{st.get('device')}  {st.get('width')}x{st.get('height')}")
            self.status_var.set("设备已就绪 · 打开右侧开关开始")
        elif ph == "probe_fail":
            self.device_lbl.configure(text="设备：未连接")
            txt = " ".join(str(st.get("text", "")).split())
            short = txt.split("→")[0].split("。")[0][:90]
            self.status_var.set(f"没探到设备：{short}（详见日志 / 控制台）")
        elif ph == "ready":
            self.device_lbl.configure(
                text=f"设备：{st.get('device')}  {st.get('width')}x{st.get('height')}")
            self._sync_locked_slots()
        elif ph in ("place", "claim"):
            self.phase_lbl.configure(text=st.get("text", ""))
            cyc = getattr(self.runner, "cycle", 0) if self.runner else 0
            self.prog_lbl.configure(text=f"第 {cyc} 轮")
            total = int(self.cycles_var.get() or 0)
            if total:
                self.prog.configure(value=min(100, cyc / total * 100))
                self.prog_lbl.configure(text=f"第 {cyc} / {total} 轮")
        elif ph == "sleep":
            self.phase_lbl.configure(text=st.get("text", ""))
            self.status_var.set(st.get("text", ""))
        elif ph == "cycle_done":
            cyc = st.get("cycle", 0)
            self.prog_lbl.configure(text=f"第 {cyc} 轮")
            total = int(self.cycles_var.get() or 0)
            if total:
                self.prog.configure(value=min(100, cyc / total * 100))
            locked = getattr(self.runner.ui, "locked_slots", set()) if self.runner else set()
            note = f" · 跳过未解锁槽位 {sorted(locked)}" if locked else ""
            self.status_var.set(f"第 {cyc} 轮完成 · 本次领到 {st.get('done', 0)} 位{note}")
        elif ph == "stopped":
            self.status_var.set("已停止")
        elif ph == "error":
            self.status_var.set("出错：" + str(st.get("text", ""))[:90])

        rec = st.get("record")
        if rec and rec.get("rarity"):
            self._add_record({
                "ts": time.strftime("%m-%d %H:%M:%S"),
                "slot": st.get("slot", 0),
                "name": rec.get("name", ""),
                "rarity": int(rec.get("rarity") or 0),
                "combo": rec.get("combo") or [],
            })

    # ------------------------------------------------------------------ 数据
    def _history_path(self):
        return paths.HISTORY_SIM_PATH if self.sim else paths.HISTORY_PATH

    def _load_total(self):
        """从 history.jsonl 汇总累计（星级 → 次数）。"""
        self.total_counts = {}
        self.opp_total = {}
        p = self._history_path()
        if not os.path.exists(p):
            self._refresh()
            return
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    r = int(rec.get("rarity") or 0)
                    if r:
                        self.total_counts[r] = self.total_counts.get(r, 0) + 1
                        if r >= 4:
                            k = (r, rec.get("name") or "未知")
                            self.opp_total[k] = self.opp_total.get(k, 0) + 1
        except OSError as e:
            self.log.warning("读历史失败: %s", e)
        self._refresh()

    def _add_record(self, rec):
        self.sess.append(rec)
        self.opp_sess[(rec["rarity"], rec["name"])] = \
            self.opp_sess.get((rec["rarity"], rec["name"]), 0) + 1

        star = rec["rarity"]
        if not (self._focus_4.get() and star < 4):
            self.tree.insert("", 0, values=self._row(rec),
                             tags=(f"r{star}" if star >= 4 else "r3",))
        self._refresh()
        if star >= 4:
            self.log.log(logging.INFO, "★ %d 星出货：%s", star, rec["name"])

    def _refresh(self):
        n = len(self.sess)
        high = sum(1 for r in self.sess if r["rarity"] >= 4)
        pct = (high / n * 100) if n else 0
        self.k_total.set(str(n), f"{n} 次")
        self.k_high.set(str(high), f"占 {pct:.0f}% · 5★{sum(1 for r in self.sess if r['rarity'] == 5)} "
                                   f"6★{sum(1 for r in self.sess if r['rarity'] == 6)}")
        if self.sess:
            best = max(self.sess, key=lambda r: r["rarity"])
            self.k_best.set(STAR_NAME.get(best["rarity"], "?"),
                            best["name"] or "—", STAR_COLOR.get(best["rarity"], COL_FG))
        tn = sum(self.total_counts.values())
        self.k_ticket.set(str(self._tickets(self.sess)),
                          f"累计 {self._tickets(self.total_counts)} / {tn} 次")
        tc = self.total_counts
        self.dist_hint.configure(
            text=f"累计 {tn} 次 · 4★+ {sum(c for r, c in tc.items() if r >= 4)}")
        self._draw_dist()
        self._draw_top()

    def _tickets(self, records):
        w = (self.cfg.get("stats") or {}).get("ticket_weights") or \
            {1: 0, 2: 0, 3: 1, 4: 1, 5: 1, 6: 1}
        w = {int(k): int(v) for k, v in w.items()}
        if isinstance(records, dict):                 # 累计：{星级: 次数}
            return sum(w.get(int(r), 0) * c for r, c in records.items())
        return sum(w.get(int(r.get("rarity") or 0), 0) for r in records)

    # ---------------------------------------------------------- 画分布图
    def _draw_dist(self):
        c = self.dist_canvas
        c.delete("all")
        w = max(c.winfo_width(), 200)
        h = max(c.winfo_height(), 100)
        counts = {}
        for r in self.sess:
            counts[r["rarity"]] = counts.get(r["rarity"], 0) + 1
        peak = max([1] + list(counts.values()))
        bw = (w - 24) / 6
        base = h - 22
        for i, star in enumerate(range(1, 7)):
            x0 = 12 + i * bw + bw * 0.18
            x1 = 12 + (i + 1) * bw - bw * 0.18
            n = counts.get(star, 0)
            bh = max(2, (base - 14) * n / peak) if n else 2
            col = STAR_COLOR[star] if n else COL_LINE
            c.create_rectangle(x0, base - bh, x1, base, fill=col, outline="")
            c.create_text((x0 + x1) / 2, base - bh - 8, text=str(n),
                          fill=COL_FG if n else COL_DIM,
                          font=("Microsoft YaHei UI", 9, "bold"))
            c.create_text((x0 + x1) / 2, base + 11, text=STAR_NAME[star],
                          fill=STAR_COLOR[star] if star >= 4 else COL_DIM,
                          font=("Microsoft YaHei UI", 9))

    # ---------------------------------------------------------- 画 4★+ 榜
    def _toggle_scope(self):
        self._scope = "total" if self._scope == "session" else "session"
        self.scope_btn.configure(text="本次" if self._scope == "total" else "累计",
                                 fg=COL_ACCENT if self._scope == "total" else COL_DIM)
        self._draw_top()

    def _draw_top(self):
        t = self.top_txt
        t.configure(state="normal")
        t.delete("1.0", "end")
        src = self.opp_total if self._scope == "total" else self.opp_sess
        items = [(k, n) for k, n in src.items() if k[0] >= 4]
        items.sort(key=lambda x: (-x[0][0], -x[1], x[0][1]))
        if not items:
            t.insert("end", "还没有 4★ 以上的出货\n", "dim")
            t.insert("end", "（刷公招时 4★ 就是主要收益，出货会自动按星级排在这里）\n", "dim")
        else:
            for (star, name), cnt in items:
                t.insert("end", f"{STAR_NAME[star]}  ", f"s{star}")
                t.insert("end", f"{name or '未知'}", f"s{star}")
                t.insert("end", f"   ×{cnt}\n", "dim")
        t.configure(state="disabled")

    @staticmethod
    def _row(rec):
        return (rec["ts"], rec["slot"], STAR_NAME.get(rec["rarity"], "?"),
                rec["name"], " + ".join(rec["combo"]) or "（未选词条）")

    def _redraw_table(self):
        """整表重画（数据量小，直接重建最不容易错）。"""
        for k in self.tree.get_children():
            self.tree.delete(k)
        only = self._focus_4.get()
        for rec in reversed(self.sess):
            if only and rec["rarity"] < 4:
                continue
            self.tree.insert("", "end", values=self._row(rec),
                             tags=(f"r{rec['rarity']}" if rec["rarity"] >= 4 else "r3",))

    # ------------------------------------------------------------------ 日志
    def _level_ok(self, level):
        mode = self._level_var.get()
        if mode == "全部":
            return True
        if mode == "常规":
            return level < logging.WARNING
        if mode == "警告+":
            return level >= logging.WARNING
        return level >= logging.ERROR

    def _append(self, level, line):
        if not self._level_ok(level):
            return
        tag = "ERROR" if level >= logging.ERROR else \
              "WARNING" if level >= logging.WARNING else "INFO"
        self.txt.configure(state="normal")
        self.txt.insert("end", line + "\n", tag)
        self._lines += 1
        if self._lines > 4000:
            self.txt.delete("1.0", "1200.0")
            self._lines -= 1200
        if not self._pause_scroll.get():
            self.txt.see("end")
        self.txt.configure(state="disabled")

    def _sync_locked_slots(self):
        """把检测到的「未解锁槽位」反映到勾选框上，用户一眼能看到为什么只跑 2 格。"""
        ui = getattr(self.runner, "ui", None)
        locked = getattr(ui, "locked_slots", set()) if ui else set()
        if not locked:
            return
        for i in sorted(locked):
            if i in self.slot_vars:
                self.slot_vars[i].set(False)
        self.status_var.set(
            f"检测到 {sorted(locked)} 号位未解锁（人力办公室等级不够）→ 已自动取消勾选")

    def _open_logs(self):
        paths.ensure_dirs()
        try:
            os.startfile(paths.LOG_DIR)
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"打不开日志目录: {e}")

    def _export_log(self):
        try:
            content = self.txt.get("1.0", "end")
            p = filedialog.asksaveasfilename(
                title="导出日志", defaultextension=".log",
                initialfile=time.strftime("recruit_%Y%m%d_%H%M%S.log"),
                filetypes=[("日志文件", "*.log"), ("文本", "*.txt")])
            if not p:
                return
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            self.status_var.set(f"日志已导出：{p}")
        except Exception as e:
            messagebox.showerror(APP_TITLE, f"导出失败: {e}")

    # ------------------------------------------------------------------ 循环
    def _pump(self):
        drained = 0
        while drained < 300:
            try:
                level, line = self.q.get_nowait()
            except queue.Empty:
                break
            self._append(level, line)
            drained += 1
        if self.worker and not self.worker.is_alive() and self._running:
            self._idle()
        self.root.after(120, self._pump)

    def _tick(self):
        if self._t0 and self._running:
            s = int(time.time() - self._t0)
            self.uptime_lbl.configure(
                text=f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}")
        self.root.after(700, self._tick)

    # ------------------------------------------------------------------ 演示
    def seed_demo(self):
        """灌一批假数据，方便调界面/截图（不写 history.jsonl）。"""
        demo = [
            (6, "能天使", ["高级资深干员"]), (5, "因陀罗", ["资深干员"]),
            (4, "豆苗", ["召唤"]), (4, "梅尔", ["召唤"]), (4, "蛇屠箱", ["输出", "群攻"]),
            (3, "玫兰莎", []), (3, "芬", []), (3, "克洛丝", []), (3, "炎熔", []),
            (4, "古米", ["输出", "群攻"]), (3, "米格鲁", []), (5, "火神", ["资深干员"]),
            (3, "空爆", []), (4, "宴", ["群攻"]), (3, "夜刀", []),
        ]
        base = time.time() - len(demo) * 90
        for i, (star, name, combo) in enumerate(demo):
            self.sess.append({
                "ts": time.strftime("%H:%M:%S", time.localtime(base + i * 90)),
                "slot": i % 4 + 1, "name": name, "rarity": star, "combo": combo,
            })
            self.opp_sess[(star, name)] = self.opp_sess.get((star, name), 0) + 1
        self.total_counts = {1: 3, 2: 12, 3: 418, 4: 226, 5: 41, 6: 4}
        self._redraw_table()
        self._refresh()


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
        ok = (not timeout) and self._lines > 5 and self.sess
        try:
            paths.ensure_dirs()
            with open(self.selftest_path, "w", encoding="utf-8") as f:
                f.write(f"{'OK' if ok else 'FAIL'} lines={self._lines} "
                        f"records={len(self.sess)} high4={sum(1 for r in self.sess if r['rarity'] >= 4)} "
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


# ====================================================================== 入口
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
    ap.add_argument("--demo", action="store_true", help="灌演示数据（调界面/截图用）")
    ap.add_argument("--no-elevate", dest="no_elevate", action="store_true",
                    help="不自动提权（调试用）")
    ap.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    paths.ensure_dirs()
    paths.ensure_user_files()          # 冻结版首次启动：自动生成 config.yaml / 数据
    cfg = load_config(paths.resolve_config(args.config))
    log = setup_logger(paths.LOG_DIR)

    if args.check_ocr:
        result = check_ocr(cfg)
        try:
            with open(os.path.join(paths.LOG_DIR, "ocr_check.txt"), "w",
                      encoding="utf-8") as f:
                f.write(result + chr(10))
        except Exception:
            pass
        print(result)
        sys.exit(0 if result.startswith("OK") else 1)

    if not args.no_elevate and not args.elevated and not args.selftest:
        try:
            from core.elevation import needs_elevation, relaunch_as_admin
            if needs_elevation(cfg):
                log.info("检测到游戏进程需要管理员权限 → 提权重启")
                if relaunch_as_admin():
                    sys.exit(0)
                log.warning("提权被取消，继续以普通权限运行（PC 端点不动）")
        except Exception as e:
            log.debug("提权检查失败: %s", e)

    if args.selftest:
        result = run_selftest(cfg, log)
        print(result)
        sys.exit(0 if result.startswith("OK") else 1)

    root = tk.Tk()
    panel = Panel(root, cfg, log)
    if args.demo:
        panel.seed_demo()
    root.mainloop()


if __name__ == "__main__":
    main()
