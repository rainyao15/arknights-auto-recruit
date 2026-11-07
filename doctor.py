"""环境自检（doctor）：不启动游戏、不发任何点击，只诊断"为什么跑不起来"。

覆盖 7 项：
  1) 运行环境   Python 版本 / 平台 / 输出编码
  2) 依赖       rapidocr / onnxruntime / opencv / numpy / yaml / requests
  3) 配置       config.yaml 关键字段、词条决策模式、票据权重
  4) 数据       recruitment.json（干员数、词条数、6★ 是否都带「高级资深干员」）
  5) OCR        渲染一张中文图 → 真跑 rapidocr → 能否认出公招词条
  6) 坐标       ui.slots / time_plus / time_minus 完整性、与 max_slots 是否匹配
  7) 设备       adb 可执行 → devices → wm size → screencap（--no-adb 可跳过）

用法：
    python doctor.py                 # 全检
    python doctor.py --no-adb        # 只查软件侧（没开模拟器时用）
    python doctor.py --config x.yaml

退出码：0 = 没有 FAIL；1 = 至少一项 FAIL。
"""
import argparse
import importlib
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)
try:                                    # Windows 控制台默认 GBK，中文/符号会炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MARK = {PASS: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}

results = []


def add(level, item, detail=""):
    results.append((level, item, detail))
    print(f"{MARK[level]} {item}" + (f"  —— {detail}" if detail else ""))


def section(title):
    print(f"\n== {title} " + "=" * max(0, 52 - len(title)))


# ---------------------------------------------------------------- 1) 运行环境
def check_env():
    section("1. 运行环境")
    v = sys.version_info
    detail = f"{v.major}.{v.minor}.{v.micro} @ {platform.platform()}"
    if v >= (3, 10):
        add(PASS, "Python 版本", detail)
    else:
        add(FAIL, "Python 版本", detail + " → 需要 3.10+（建议 3.12/3.13）")

    if os.name == "nt":
        add(PASS, "操作系统", "Windows")
    else:
        add(WARN, "操作系统", f"{os.name} —— 脚本按 Windows/模拟器写的，其他平台需自行改路径")

    enc = (sys.stdout.encoding or "").lower()
    if "utf" in enc:
        add(PASS, "控制台编码", enc)
    else:
        add(WARN, "控制台编码", f"{enc} —— 中文可能乱码，可先执行 chcp 65001")

    if shutil.which("python") or shutil.which("py"):
        add(PASS, "python 在 PATH", shutil.which("python") or shutil.which("py"))
    else:
        add(WARN, "python 在 PATH", "没找到，双击 .bat 时可能用错解释器")


# ---------------------------------------------------------------- 2) 依赖
DEPS = [
    ("rapidocr", "OCR 引擎"),
    ("onnxruntime", "推理后端"),
    ("cv2", "opencv-python-headless（截图解码）"),
    ("numpy", "数组/图像"),
    ("yaml", "PyYAML（配置）"),
    ("requests", "拉取词条数据"),
]


def check_deps():
    section("2. 依赖")
    missing = []
    for mod, desc in DEPS:
        try:
            m = importlib.import_module(mod)
            ver = getattr(m, "__version__", "")
            add(PASS, f"{mod}", f"{desc}{' ' + str(ver) if ver else ''}")
        except Exception as e:
            missing.append(mod)
            add(FAIL, f"{mod}", f"{desc} 导入失败: {e}")
    if missing:
        add(WARN, "修复建议",
            "python -m pip install -r requirements.txt "
            "-i https://pypi.tuna.tsinghua.edu.cn/simple")
    return {m: m not in missing for m, _ in DEPS}


# ---------------------------------------------------------------- 3) 配置
def check_config(cfg_path):
    section("3. 配置")
    from core.config import load_config
    if not os.path.exists(cfg_path):
        add(FAIL, "config.yaml", f"不存在: {cfg_path}")
        return None
    try:
        cfg = load_config(cfg_path)
        add(PASS, "config.yaml", os.path.abspath(cfg_path))
    except Exception as e:
        add(FAIL, "config.yaml", f"解析失败: {e}")
        return None

    adb = cfg.get("adb", {}) or {}
    path = adb.get("adb_path") or ""
    if path and not os.path.exists(path):
        add(FAIL, "adb.adb_path", f"路径不存在: {path}")
    elif path:
        add(PASS, "adb.adb_path", path)
    else:
        add(PASS, "adb.adb_path", "留空（自动探测 PATH / 模拟器自带 adb）")
    ports = adb.get("ports") or []
    add(PASS if ports else FAIL, "adb.ports", str(ports) if ports else "为空，无法探测模拟器")

    dec = cfg.get("decision", {}) or {}
    mode = dec.get("mode", "basic")
    fb = dec.get("fallback", "none")
    add(PASS if mode in ("basic", "lock") else FAIL, "decision.mode", mode)
    add(PASS if fb in ("none", "best_tag") else FAIL, "decision.fallback", fb)
    if mode == "basic" and fb == "none":
        add(PASS, "决策口径", "保底优先 + 无特殊词条不选词条（刷票最优）")

    g = cfg.get("game", {}) or {}
    hours = g.get("recruit_time_hours", 9)
    add(PASS if 1 <= hours <= 9 else FAIL, "game.recruit_time_hours", f"{hours}h")
    how = g.get("on_expedite_exhausted", "stop")
    add(PASS if how in ("stop", "natural") else FAIL, "加急耗尽策略", how)

    st = cfg.get("stats", {}) or {}
    w = st.get("ticket_weights") or {}
    bad = [k for k in (1, 2, 3, 4, 5, 6) if int(k) not in {int(x) for x in w}]
    add(PASS if not bad and w else WARN, "stats.ticket_weights",
        f"1★~6★: {dict(sorted((int(k), v) for k, v in w.items()))}" if w
        else "未配置，用默认权重")

    n = cfg.get("notify", {}) or {}
    if n.get("enabled") and not (n.get("smtp_password") or "").strip():
        add(WARN, "notify.smtp_password", "为空 → 邮件推送会静默失败（只写日志）")
    elif n.get("enabled"):
        add(PASS, "notify.smtp_password", "已填写授权码（不打印内容）")
    else:
        add(PASS, "notify", "推送已关闭")
    return cfg


# ---------------------------------------------------------------- 4) 数据
def check_data(cfg):
    section("4. 词条数据")
    if cfg is None:
        add(WARN, "词条数据", "配置未加载，跳过")
        return None
    from core.data_loader import TAGS_31, DataLoader
    try:
        data = DataLoader(cfg).load()
    except Exception as e:
        add(FAIL, "加载 recruitment.json", f"{e}")
        return None

    if data.operators:
        add(PASS, "干员数据", f"{len(data.operators)} 个干员")
    else:
        add(WARN, "干员数据", "空 —— 决策会退化为纯词条规则（保底逻辑仍正确）")

    if data.tags:
        add(PASS, "词条数据", f"{len(data.tags)} 个词条")
    else:
        add(WARN, "词条数据", "空 —— 用内置 TAGS_31 兜底")

    six = data.by_rarity.get(6, [])
    if six:
        missing = [o["name"] for o in six if "高级资深干员" not in o.get("tags", [])]
        add(PASS if not missing else WARN, "6★ 词条校验",
            f"{len(six)} 个 6★ 都带「高级资深干员」" if not missing
            else f"有 {len(missing)} 个 6★ 未标高级资深: {missing[:5]}")
    five = data.by_rarity.get(5, [])
    if five:
        missing5 = [o["name"] for o in five if "资深干员" not in o.get("tags", [])]
        add(PASS if not missing5 else WARN, "5★ 词条校验",
            f"{len(five)} 个 5★ 都带「资深干员」" if not missing5
            else f"有 {len(missing5)} 个 5★ 未标资深: {missing5[:5]}")

    add(PASS, "内置词条白名单", f"TAGS_31 = {len(TAGS_31)} 个（官方 31 词条）")
    return data


# ---------------------------------------------------------------- 5) OCR
def _font(size=44):
    """找一个能渲染中文的字体。"""
    from PIL import ImageFont
    for p in (
        r"C:/Windows/Fonts/msyh.ttc", r"C:/Windows/Fonts/msyhbd.ttc",
        r"C:/Windows/Fonts/simhei.ttf", r"C:/Windows/Fonts/simsun.ttc",
        r"C:/Windows/Fonts/Deng.ttf",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size), p
            except Exception:
                continue
    return None, None


def check_ocr(cfg):
    section("5. OCR（真跑一次 rapidocr）")
    if cfg is None:
        add(WARN, "OCR", "配置未加载，跳过")
        return
    try:
        from PIL import Image, ImageDraw
        import numpy as np
    except Exception as e:
        add(FAIL, "PIL/numpy", f"{e}")
        return

    font, fpath = _font()
    want = ["高级资深干员", "资深干员", "狙击干员"]
    img = Image.new("RGB", (900, 240), (24, 26, 32))
    d = ImageDraw.Draw(img)
    if font is not None:
        d.text((20, 20), "高级资深干员  资深干员", font=font, fill=(245, 245, 245))
        d.text((20, 110), "狙击干员  输出  远程位", font=font, fill=(245, 245, 245))
        add(PASS, "中文字体", os.path.basename(fpath))
    else:
        add(WARN, "中文字体", "没找到系统中文字体，OCR 自检可能失真")

    arr = np.array(img)[:, :, ::-1].copy()     # RGB → BGR
    t0 = time.time()
    try:
        from core.ocr import OcrEngine
        from core.tag_recognizer import extract_tags
        eng = OcrEngine(cfg)
        txts = [t.text for t in eng.recognize(arr)]
        cost = time.time() - t0
        tags = extract_tags(txts, threshold=cfg["ocr"]["match_threshold"])
        hit = [w for w in want if w in tags]
        detail = f"识别 {txts} → 词条 {tags}（{cost:.1f}s）"
        if len(hit) >= 2:
            add(PASS, "OCR 识别", detail)
        elif hit:
            add(WARN, "OCR 识别", detail + " —— 部分命中，真机首跑建议盯一眼")
        else:
            add(FAIL, "OCR 识别", detail + " —— rapidocr 模型异常，重装 rapidocr/onnxruntime")
        if cost > 15:
            add(WARN, "OCR 速度", f"首帧 {cost:.1f}s（模型加载），实际运行会快很多")
    except Exception as e:
        add(FAIL, "OCR 识别", f"{type(e).__name__}: {e}")
        traceback.print_exc()


# ---------------------------------------------------------------- 6) 坐标
def check_ui(cfg):
    section("6. UI 坐标")
    if cfg is None:
        add(WARN, "UI 坐标", "配置未加载，跳过")
        return
    ui = cfg.get("ui", {}) or {}
    slots = ui.get("slots") or []
    max_slots = (cfg.get("game", {}) or {}).get("max_slots", 4)
    if not slots:
        add(FAIL, "ui.slots", "空 —— 先跑 python calibrate.py 采 4 个公招位坐标")
    elif len(slots) < max_slots:
        add(WARN, "ui.slots",
            f"配了 {len(slots)} 个，game.max_slots={max_slots} → 只会处理前 {len(slots)} 位")
    else:
        add(PASS, "ui.slots", f"{len(slots)} 个（max_slots={max_slots}）")
    bad = [s for s in slots if not (0 <= s.get("x", -1) <= 1 and 0 <= s.get("y", -1) <= 1)]
    if bad:
        add(FAIL, "ui.slots 取值范围", f"有 {len(bad)} 个坐标不在 0~1，示例: {bad[0]}")

    for key in ("time_plus", "time_minus"):
        p = ui.get(key)
        ok = isinstance(p, dict) and 0 <= p.get("x", -1) <= 1 and 0 <= p.get("y", -1) <= 1
        add(PASS if ok else FAIL, f"ui.{key}", f"{p}" if ok else "缺失或越界（时长调节要用）")

    step = ui.get("time_step_min", 10)
    taps = (9 * 60 - 60) // max(1, step) if step else 0
    add(PASS, "时长调节", f"每次 +{step} 分钟，60min→9h 约点 {taps} 次")


# ---------------------------------------------------------------- 7) 设备
def check_adb(cfg):
    section("7. 设备 / ADB")
    from core.adb_client import ADB_HINTS, AdbClient, AdbError

    explicit = ((cfg or {}).get("adb", {}) or {}).get("adb_path") or "" if cfg else ""
    if explicit:
        add(PASS if os.path.exists(explicit) else FAIL, "adb 可执行", explicit or "-")
        if not os.path.exists(explicit):
            return
    else:
        found = shutil.which("adb")
        if found:
            add(PASS, "adb 可执行", found + "（来自 PATH）")
        else:
            hit = next((h for h in ADB_HINTS if os.path.exists(h)), None)
            if hit:
                add(PASS, "adb 可执行", hit + "（模拟器自带）")
            else:
                add(FAIL, "adb 可执行",
                    "PATH 和常见模拟器目录都没找到 → 在 config.yaml 填 adb.adb_path")
                return

    resolved = (explicit or shutil.which("adb")
                or next((h for h in ADB_HINTS if os.path.exists(h)), ""))
    check_adb_conflict(resolved)

    try:
        client = AdbClient(cfg)
    except AdbError as e:
        add(FAIL, "adb 初始化", str(e))
        return
    try:
        serial = client.connect()
        add(PASS, "连接设备", serial)
    except AdbError as e:
        add(FAIL, "连接设备", f"{str(e)}（模拟器没开 / 端口不对 / 未开 ADB 调试）")
        return

    try:
        w, h = client.wm_size()
        add(PASS, "分辨率", f"{w}x{h}")
    except AdbError as e:
        add(FAIL, "分辨率", str(e))

    try:
        start = time.time()
        img = client.screencap()
        add(PASS, "截图", f"{img.shape[1]}x{img.shape[0]}（{time.time() - start:.1f}s）")
    except Exception as e:
        add(FAIL, "截图", f"{type(e).__name__}: {e}")
        return

    try:
        pkg = client.current_package()
        if pkg:
            add(PASS, "前台应用", pkg)
        else:
            add(WARN, "前台应用", "读不到（可能停在桌面）")
    except AdbError as e:
        add(WARN, "前台应用", str(e))

    # 顺手把这张截图存下来，方便目测设备是不是真在游戏界面
    try:
        import cv2
        os.makedirs("logs", exist_ok=True)
        p = os.path.join("logs", "doctor_shot.png")
        cv2.imwrite(p, img)
        add(PASS, "截图存档", p)
    except Exception as e:
        add(WARN, "截图存档", str(e))


# ---------------------------------------------------------------- 8) 模拟器渲染
# 2026-11 实测：MuMu 15.x / Android 15 / AMD Vega8 / 明日方舟 2.7.91
# 这一组是“游戏能在模拟器里活下来”的最小组合，改错任何一个都是 3 秒内 SIGSEGV。
MUMU_MANAGER_HINTS = [
    r"D:/Program Files/Netease/MuMu/nx_main/MuMuManager.exe",
    r"C:/Program Files/Netease/MuMu/nx_main/MuMuManager.exe",
    r"D:/Program Files/Netease/MuMuPlayer-15.0/nx_main/MuMuManager.exe",
]
GOOD_RENDER = {
    "renderer_mode": ("dx", "vk → 宿主 amdvlk64.dll 崩，整个模拟器直接退出（错误码 901）"),
    "force_discrete_graphics": ("false", "true → 游戏起来 3 秒后 libunity 空指针 SIGSEGV"),
    "renderer_strategy": ("dis", "auto / perf → 游戏起来 3 秒后 libunity 空指针 SIGSEGV"),
}


def _find_mumumanager():
    for p in MUMU_MANAGER_HINTS:
        if os.path.exists(p):
            return p
    return None


def _mumu_settings(mm, keys):
    import json
    cmd = [mm, "setting", "-v", "0"]
    for k in keys:
        cmd += ["-k", k]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60, text=True,
                           encoding="utf-8", errors="replace")
        return json.loads(r.stdout or "{}")
    except Exception as e:
        add(WARN, "读取模拟器设置", f"{type(e).__name__}: {e}")
        return {}


def _mumu_set(mm, key, val):
    import json
    try:
        r = subprocess.run([mm, "setting", "-v", "0", "-k", key, "-val", str(val)],
                           capture_output=True, timeout=60, text=True,
                           encoding="utf-8", errors="replace")
        data = json.loads(r.stdout or "{}")
        return str(data.get(key, "")).lower() == str(val).lower()
    except Exception:
        return False


def _adb_version(path):
    try:
        r = subprocess.run([path, "version"], capture_output=True, timeout=20, text=True,
                           encoding="utf-8", errors="replace")
        m = re.search(r"Version (\d+\.\d+\.\d+)", r.stdout or "")
        return m.group(1) if m else ""
    except Exception:
        return ""


def check_adb_conflict(active=""):
    """一台机器上装了多个版本的 adb，会互相 kill server。"""
    from core.adb_client import ADB_HINTS
    cands = set()
    for p in [active, shutil.which("adb") or ""] + ADB_HINTS:
        if p and os.path.exists(p):
            cands.add(os.path.normpath(p))
    vers = {p: _adb_version(p) for p in sorted(cands)}
    vers = {p: v for p, v in vers.items() if v}
    if len(set(vers.values())) > 1:
        detail = " | ".join(f"{v} ← {p}" for p, v in vers.items())
        add(WARN, "adb 版本冲突",
            detail + "\n         多个版本会抢 5037，现象是设备反复 offline / adb: waiting for device。"
            "\n         修法：config.yaml 里 adb.adb_path 只指向一个，其余 adb.exe 别去调。")
    elif vers:
        add(PASS, "adb 版本唯一", f"{next(iter(vers.values()))}（{next(iter(vers))}")


def check_emulator(fix=False):
    section("8. 模拟器渲染（MuMu 15.x）")
    mm = _find_mumumanager()
    if not mm:
        add(WARN, "MuMuManager", "没找到（不是 MuMu 15 或装在别的盘）→ 本项跳过")
        return
    add(PASS, "MuMuManager", mm)
    cur = _mumu_settings(mm, list(GOOD_RENDER))
    bad = []
    for k, (want, why) in GOOD_RENDER.items():
        got = str(cur.get(k, "")).lower()
        if got == want:
            add(PASS, k, got)
        else:
            add(WARN, k, f"{got or '?'} → 建议 {want}（{why}）")
            bad.append(k)
    if not bad:
        return
    if fix:
        for k in bad:
            ok = _mumu_set(mm, k, GOOD_RENDER[k][0])
            add(PASS if ok else FAIL, f"修复 {k}", "已写入" if ok else "写入失败，用 MuMu 设置面板手改")
        add(WARN, "需要重启模拟器",
            f'{mm} control -v 0 shutdown && "{mm}" control -v 0 launch')
    else:
        add(WARN, "一键修复", "加 --fix-emulator 参数自动写回推荐值（写完要重启模拟器）")


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="明日方舟自动公招 环境自检")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--no-adb", action="store_true", help="跳过设备检查（没开模拟器时用）")
    ap.add_argument("--fix-emulator", action="store_true",
                    help="把 MuMu 渲染设置写回能跑游戏的值（写完要重启模拟器）")
    args = ap.parse_args()

    print("=" * 60)
    print("明日方舟自动公招 · 环境自检 (doctor)")
    print("=" * 60)

    check_env()
    deps_ok = check_deps()
    cfg = check_config(args.config)
    check_data(cfg)
    if deps_ok.get("rapidocr") and deps_ok.get("cv2"):
        check_ocr(cfg)
    else:
        add(WARN, "OCR 识别", "依赖缺失，跳过真跑")
    check_ui(cfg)
    if args.no_adb:
        section("7. 设备 / ADB")
        add(WARN, "设备检查", "已按 --no-adb 跳过")
    else:
        check_adb(cfg)
    check_emulator(args.fix_emulator)

    n_fail = sum(1 for lv, _, _ in results if lv == FAIL)
    n_warn = sum(1 for lv, _, _ in results if lv == WARN)
    print("\n" + "=" * 60)
    print(f"结果：{len(results) - n_fail - n_warn} 项通过 · {n_warn} 项警告 · {n_fail} 项失败")
    if n_fail:
        print("先修 FAIL 项。设备类 FAIL 不影响 `python main.py --dry-run` 离线验证。")
    elif n_warn:
        print("可以跑了；WARN 项按提示逐条确认即可。")
    else:
        print("全绿。直接 `python main.py` 开跑。")
    print("=" * 60)
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
