"""PC 端体检 / 标定工具（对应模拟器时代的 doctor.py + probe.py + calibrate.py）。

用法（在项目根目录）：

    python pc_probe.py --list                  # 看机器上所有窗口：标题/类名/进程/尺寸
    python pc_probe.py --shot logs/pc.png      # 抓一张游戏窗口图，确认不是黑屏
    python pc_probe.py --bench                 # 抓屏耗时基准（10 次）
    python pc_probe.py --diff                  # 对比 sendinput / postmessage 哪种点击游戏认
    python pc_probe.py --click 0.5 0.5         # 用比例坐标点一下，看游戏有没有反应
    python pc_probe.py --grid                  # 依次点 4 个公招位（验证 config.ui.slots）
    python pc_probe.py --verify                # 跑极速裁块 OCR：词条/时长/出货名能不能读到

标定公招位坐标继续用：python calibrate.py（后端自动切到 PC，截图来自游戏窗口）
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

import cv2
import numpy as np

from core import paths
from core.config import load_config


def _connect(cfg):
    from core.pc_client import PcClient
    c = PcClient(cfg)
    c.connect()
    return c


def cmd_list(cfg):
    from core.pc_client import PcClient
    c = PcClient(cfg)
    rows = c.list_windows()
    print(f"{'hwnd':>10}  {'size':>10}  {'class':<22} {'exe':<24} title")
    for hwnd, title, cls, pid, exe, w, h in rows:
        print(f"{hwnd:>10}  {f'{w}x{h}':>10}  {cls[:22]:<22} "
              f"{os.path.basename(exe)[:24]:<24} {title[:40]}")
    hit = c.find_window()
    print("\n匹配到:", hit[:4] if hit else "无（把上面的 title / class / exe 抄进 config.yaml 的 pc.*）")


def cmd_shot(cfg, out):
    c = _connect(cfg)
    t0 = time.time()
    img = c.screencap()
    dt = (time.time() - t0) * 1000
    print(f"窗口 {c.serial} 客户区 {img.shape[1]}x{img.shape[0]}  抓屏 {dt:.0f}ms")
    if img.mean() < 3:
        print("⚠ 全黑：换 capture: printwindow 或 gdi；全屏独占模式抓不到，改无边框窗口")
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    cv2.imwrite(out, img)
    print("已保存", out)


def cmd_bench(cfg, n=10):
    c = _connect(cfg)
    ds = []
    for _ in range(n):
        t0 = time.time()
        c.screencap()
        ds.append((time.time() - t0) * 1000)
    ds.sort()
    print(f"抓屏 {n} 次: 最快 {ds[0]:.0f}ms  中位 {ds[len(ds)//2]:.0f}ms  最慢 {ds[-1]:.0f}ms")
    print("参考：ADB exec-out 约 660ms；PC 端 <30ms 才算合格，>100ms 换 capture: mss")
    t0 = time.time()
    w, h = c.wm_size()
    print(f"wm_size -> {w}x{h}  ({(time.time()-t0)*1000:.0f}ms)")


def _diff_score(a, b):
    if a is None or b is None or a.shape != b.shape:
        return 0.0
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def cmd_diff(cfg):
    """验证哪种输入方式游戏真的接收：点屏幕中央，前后帧差异越大越说明点到了。"""
    c = _connect(cfg)
    w, h = c.wm_size()
    print(f"窗口 {w}x{h}，在中央点一下，比较两种输入方式的画面变化\n")
    for mode in ("sendinput", "postmessage"):
        c.input_mode = mode
        before = c.screencap()
        c.tap(w / 2, h / 2)
        time.sleep(0.7)
        after = c.screencap()
        d = _diff_score(before, after)
        verdict = "✅ 游戏有反应" if d > 2.0 else "❌ 画面没变（这种输入被忽略）"
        print(f"{mode:<12} 帧差 {d:6.2f}  {verdict}")
    print("\n结论写回 config.yaml → pc.input")


def cmd_click(cfg, px, py):
    c = _connect(cfg)
    w, h = c.wm_size()
    x, y = px * w, py * h
    before = c.screencap()
    print(f"点击比例 ({px}, {py}) → 像素 ({x:.0f}, {y:.0f})  [{c.input_mode}]")
    c.tap(x, y)
    time.sleep(0.8)
    after = c.screencap()
    d = _diff_score(before, after)
    print(f"帧差 {d:.2f} " + ("→ 点到了" if d > 2.0 else "→ 没反应（坐标不对 / 输入方式不对 / 窗口没焦点）"))
    cv2.imwrite(os.path.join(paths.LOG_DIR, "pc_click_before.png"), before)
    cv2.imwrite(os.path.join(paths.LOG_DIR, "pc_click_after.png"), after)
    print("前后对比图在 logs/pc_click_{before,after}.png")


def cmd_grid(cfg):
    """按 config.ui.slots 依次点公招位，适合在公招列表页验证坐标。"""
    from core.ocr import OcrEngine
    c = _connect(cfg)
    ocr = OcrEngine(cfg)
    w, h = c.wm_size()
    for i, s in enumerate(cfg["ui"].get("slots") or [], 1):
        x, y = s["x"] * w, s["y"] * h
        c.tap(x, y)
        time.sleep(0.9)
        texts = ocr.texts(c.screencap())
        print(f"槽位{i} ({x:.0f},{y:.0f}) → {texts[:8]}")
        cv2.imwrite(os.path.join(paths.LOG_DIR, f"pc_slot{i}.png"), c.screencap())
        c.tap(0.05 * w, 0.05 * h)      # 左上返回，关面板
        time.sleep(0.4)


def cmd_verify(cfg):
    """跑极速裁块：直接复用 ui.fast 的坐标，确认 1600x900 那套标定在 PC 上还准。"""
    from core.ocr import FastOcr
    from core.tag_recognizer import extract_tags
    from core.data_loader import DataLoader
    c = _connect(cfg)
    w, h = c.wm_size()
    print(f"客户区 {w}x{h}  " + ("（== 1600x900，极速路径可用）" if (w, h) == (1600, 900)
                                else "（≠ 1600x900：建议把游戏窗口改成 1600x900）"))
    img = c.screencap()
    fast = cfg["ui"].get("fast") or {}
    focr = FastOcr()

    def crop(node):
        hh, ww = img.shape[:2]
        return img[int(node["y1"] * hh):int(node["y2"] * hh),
                   int(node["x1"] * ww):int(node["x2"] * ww)]

    for key in ("time", "header", "list_head", "label", "name"):
        node = fast.get(key)
        if not node:
            continue
        sub = crop(node)
        if sub.size == 0:
            print(f"{key:<10} 裁块为空（坐标越界）")
            continue
        print(f"{key:<10} {sub.shape[1]}x{sub.shape[0]} → {focr.line(sub)!r}")
        cv2.imwrite(os.path.join(paths.LOG_DIR, f"pc_fast_{key}.png"), sub)

    tags_node = fast.get("tag_grid")
    if tags_node:
        data = DataLoader(cfg).load()
        hh, ww = img.shape[:2]
        out = []
        for r1, r2 in tags_node["rows"]:
            for c1, c2 in tags_node["cols"]:
                cell = img[int(r1 * hh):int(r2 * hh), int(c1 * ww):int(c2 * ww)]
                t = focr.line(cell)
                if t:
                    out.append(t)
        hits = extract_tags(out, tags=list(data.tags.keys()) or None,
                            threshold=cfg["ocr"]["match_threshold"])
        print(f"tag_grid 读到 {out} → 命中词条 {hits}")
    print("\n如果时间/词条读不出来：先确认在「词条面板」，再按 logs/pc_fast_*.png 微调 config.ui.fast")


def cmd_flow_check(cfg):
    """PC 端「只读跑一轮」：不提交招募、不消耗任何资源，只验证点得动 + 读得准。

    安全闸：
      - 战斗/结算界面直接退出（不点任何坐标）；
      - 只有 OCR 明确看到大厅有「公开招募」才点；
      - 打开槽位后只读词条、跑决策，然后取消面板。
    """
    from core.data_loader import DataLoader
    from core.decision import decide
    from core.device import create_backend
    from core.ocr import OcrEngine
    from core.ui import UI

    BACKEND = create_backend(cfg)
    BACKEND.connect()
    ocr = OcrEngine(cfg)
    ui = UI(BACKEND, ocr, cfg, DataLoader(cfg).load())
    print(f"设备 {BACKEND.serial}  分辨率 {ui.width}x{ui.height}  "
          f"极速路径 {'可用' if ui.fast_ready() else '不可用（走慢速 OCR）'}")

    img = ui.shot()
    texts = ocr.texts(img)
    battle_kw = ["剩余可放置角色", "ENEMY", "行动结束", "暂  停", "自动部署", "理智"]
    if any(any(k in t for k in battle_kw) for t in texts):
        print("❌ 当前在战斗/结算界面，不点任何坐标。先回主界面再跑 --flow-check")
        print("   识别到:", texts[:12])
        return
    print("当前界面:", texts[:10])

    if not any(any(k in t for k in ["公开招募", "RECRUIT", "RECRUITMENT"]) for t in texts):
        print("❌ 没在大厅（看不到「公开招募」入口）→ 不动坐标。请手动回到主界面")
        return

    print()
    print("① 进公开招募页")
    try:
        ui.ensure_recruit_screen()
    except Exception as e:
        print("❌ 进公招失败:", e)
        ui.save_shot("pc_flow_no_recruit")
        return
    print("✅ 已进入公招页")

    print("② 打开第 1 个公招位，读词条")
    try:
        ui.open_slot(1)
        time.sleep(0.8)
        ui._last_img = None
        tags = ui.read_tags_fast() if ui.fast_ready() else ui.read_tags()
        if len(tags) < 2:
            tags = ui.read_tags()
    except Exception as e:
        print("❌ 读词条失败:", e)
        ui.save_shot("pc_flow_read_tags")
        ui.cancel_panel()
        return
    print("   词条:", "、".join(tags) if tags else "（无）")

    print("③ 决策（不提交）")
    combo, reason = decide(tags, mode=cfg["decision"].get("mode", "basic"),
                           operators=DataLoader(cfg).load().operators,
                           fallback=cfg["decision"].get("fallback", "none"))
    print("   选择:", "+".join(combo) if combo else "（不选词条）")
    print("   理由:", reason)

    print("④ 关掉面板（不提交、不消耗）")
    ui.cancel_panel()
    ui.save_shot("pc_flow_done")
    print()
    print("✅ 只读链路通了：截图 → OCR → 坐标点击 → 词条识别 → 决策 → 取消")
    print("   下一步就能直接 `python main.py`（记得管理员 + 游戏前台）")


def cmd_diag(cfg):
    from core.pc_client import PcClient, is_admin
    icon = {"OK": "✅", "WARN": "⚠ ", "FAIL": "❌"}
    print("=" * 62)
    print("明日方舟 PC 端 · 环境体检")
    print("=" * 62)
    c = PcClient(cfg)
    try:
        c.connect()
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        print("   先跑 `python pc_probe.py --list` 看窗口真名，抄进 config.yaml → pc.*")
        return
    rows = c.diagnose()
    for lvl, txt in rows:
        print(f"{icon.get(lvl, '  ')} {txt}")
    bad = [r for r in rows if r[0] == "FAIL"]
    print("-" * 62)
    if bad:
        print("有 %d 项不过 → 先修这些，再谈点击准不准" % len(bad))
    else:
        print("体检全绿。下一步：")
        print("  1) python pc_probe.py --diff     # 确认 sendinput / postmessage 哪种游戏认")
        print("  2) python calibrate.py           # 重标 4 个公招位（窗口调成 1600x900 后）")
        print("  3) python pc_probe.py --verify   # 验证 ui.fast 裁块能不能读到文字")
        print("  4) python main.py --once         # 只跑一轮看日志")


def main():
    ap = argparse.ArgumentParser(description="明日方舟 PC 端体检/标定")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--shot", nargs="?", const=os.path.join("logs", "pc.png"))
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--click", nargs=2, type=float, metavar=("X", "Y"), help="比例坐标 0~1")
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--diag", action="store_true", help="一键体检：权限/窗口/抓屏/焦点")
    ap.add_argument("--flow-check", dest="flow_check", action="store_true",
                    help="只读跑一轮公招（进页→开槽→读词条→决策→取消，不提交不消耗）")
    args = ap.parse_args()

    paths.ensure_dirs()
    cfg = load_config(os.path.abspath(args.config))

    if args.list:
        return cmd_list(cfg)
    if args.shot:
        return cmd_shot(cfg, args.shot)
    if args.bench:
        return cmd_bench(cfg)
    if args.diff:
        return cmd_diff(cfg)
    if args.click:
        return cmd_click(cfg, *args.click)
    if args.grid:
        return cmd_grid(cfg)
    if args.verify:
        return cmd_verify(cfg)
    if args.diag:
        return cmd_diag(cfg)
    if args.flow_check:
        return cmd_flow_check(cfg)
    ap.print_help()


if __name__ == "__main__":
    main()
