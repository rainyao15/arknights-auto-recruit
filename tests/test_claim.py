"""领取阶段（claim_card_fast）的回归测试。

覆盖 2026-11-08 实机事故（logs/shot_142605_claim3_n1_other.png）：

  四格挂满后，1/2/3 号位的加急券被静默点掉（卡片变成「已找到候选人」），
  但一次「聘用候选人」都没点，最后 13s 超时判 unknown，面板显示「2 轮完成、0 出货」。

两个已证实的原因：

  1) 读带（band）锚在「立即招募」的点上，而「聘用候选人」文字在卡片中线，
     实测两者中心差 0.07~0.12 屏宽 → 窄带把文字裁成 '人' → 关键字不命中；
  2) 列表页标题被读成「公开招莫」→ screen_kind 判 other → 在列表页上点中央（空点）。

这里用「几何 + 假屏」把这两条钉死，不依赖真机、不依赖 OCR 模型。

跑法：
    python tests/test_claim.py
    python -m pytest tests/test_claim.py -q
"""
import os
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core.config import load_config
from core.data_loader import DataLoader
from core.logger import setup_logger
from core.ui import UI

CFG = load_config("config.yaml")
DATA = DataLoader(CFG).load()
LOG = setup_logger(os.path.join(tempfile.mkdtemp(), "logs"))


class FakeAdb:
    """只记录 tap，不出图：screencap 给一张 900x1600 的纯黑帧。"""

    serial = "sim-claim-test"

    def __init__(self):
        self.taps = []

    def screencap(self):
        return np.zeros((900, 1600, 3), np.uint8)

    def wm_size(self):
        return (1600, 900)

    def tap(self, x, y):
        self.taps.append((round(x), round(y)))

    def tap_many(self, pts, gap_ms=20):
        self.taps.extend((round(x), round(y)) for x, y in pts)

    def swipe(self, *a, **k):
        pass

    def current_package(self):
        return "com.hypergryph.arknights"


class FakeOcr:
    def texts(self, img):
        return []

    def recognize(self, img):
        return []


def _ui():
    adb = FakeAdb()
    ui = UI(adb, FakeOcr(), CFG, DATA)
    # 不走真 OCR / 真界面判断，全部按用例喂
    ui._last_img = np.zeros((900, 1600, 3), np.uint8)
    return ui, adb


# ---------------- 1) 几何：读带必须能盖住「聘用候选人」 ----------------
def test_claim_band_covers_both_button_centers():
    """读带以 exp 点为中心 ±0.05 时盖不住居中的领人按钮 —— 这就是事故根因。

    修好后：band 的左右边界必须同时包住「立即招募」点(bx) 和「聘用候选人」点(cx)。
    """
    ui, _ = _ui()
    w = ui.width
    for slot in (1, 2, 3, 4):
        bx, _by = ui.card_button(slot)
        cx, _cy = ui.card_center(slot)
        x1 = min(bx / w, cx / w) - 0.10
        x2 = max(bx / w, cx / w) + 0.10
        assert x1 * w <= bx <= x2 * w
        assert x1 * w <= cx <= x2 * w, f"第 {slot} 格读带盖不住领人按钮"
        # 旧写法（只盖 exp 点 ±0.05）必须被判定为不够：两者中心确实差得远
        old_x1, old_x2 = bx / w - 0.05, bx / w + 0.05
        assert not (old_x1 * w <= cx <= old_x2 * w), \
            f"第 {slot} 格 exp/claim 中心重叠了，本用例已失效，请重新标定"


# ---------------- 2) 卡上已是「聘用候选人」→ 必须点卡片中线 ----------------
def test_claim_taps_center_when_card_already_claimable():
    ui, adb = _ui()
    cx, _cy = ui.card_center(1)
    _bx, by = ui.card_button(1)

    ui._sync_to_list = lambda tries=3: "list"
    ui._card_button = lambda slot: "claim"          # 复核结论：聘用候选人
    ui._rec = lambda node, img=None: ""
    ui.on_result_card = lambda: bool(adb.taps)      # 点完就当已到出货卡

    status = ui.claim_card_fast(1)
    assert status == "ok", status
    assert adb.taps, "一次都没点"
    assert adb.taps[0] == (round(cx), round(by)), f"该点卡片中线，实际点了 {adb.taps[0]}"


# ---------------- 3) 卡上还是「立即招募」→ 补点加急，而不是干等 ----------------
def test_claim_retries_expedite_when_still_running():
    ui, adb = _ui()
    _cx, _ = ui.card_center(4)
    bx, by = ui.card_button(4)

    ui._sync_to_list = lambda tries=3: "list"
    ui._card_button = lambda slot: "expedite"
    ui._wait_expedite_dialog = lambda timeout=3.0: "timeout"   # 弹窗没读到
    seen = {"n": 0}

    def _rec(node, img=None):
        # 第一拍仍读到「立即招募」→ 触发补点；补点后当已到出货卡
        seen["n"] += 1
        return "立即招募" if seen["n"] == 1 else ""

    ui._rec = _rec
    ui.on_result_card = lambda: seen["n"] > 1

    status = ui.claim_card_fast(4)
    assert status == "ok", status
    assert adb.taps.count((round(bx), round(by))) >= 2, f"没有补点加急: {adb.taps}"


# ---------------- 4) 列表页标题读错一个字也不能判 other ----------------
def test_screen_kind_tolerates_misread_header():
    ui, _ = _ui()

    def _rec(node, img=None):
        # 实测：旧裁块把「公开招募」读成「公开招莫」
        return "公开招莫" if node.get("x1", 0) < 0.05 else ""

    ui._rec = _rec
    ui._dialog_text = lambda img=None, deep=False: ""
    ui.on_result_card = lambda: False
    ui.on_lobby = lambda: False
    ui.on_base_screen = lambda: False
    assert ui.screen_kind() == "list"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("全部通过")


# ---------------- 5) 绝不能把「停止招募」确认框当成加急确认 ----------------
def test_stop_dialog_taps_cancel_never_confirm():
    """2026-11-08 实机：脚本点到「停止招募」→ 弹确认框 → 老代码把右侧
    (0.754,0.704) 当成加急 ✓ 点下去，正好是「确认停止」红按钮，招募被放弃。"""
    from core.ocr import OcrText
    ui, adb = _ui()
    ui.dialogs_seen = 0

    def fake_dialog(img=None, deep=False):
        return "停止招募将放弃已投入的资源并刷新当前标签列表，是否确认停止招募?"

    ui._dialog_text = fake_dialog
    ui.shot = lambda: np.zeros((900, 1600, 3), np.uint8)
    got = ui._wait_expedite_dialog(timeout=1.0)
    assert got == "stopped", got
    assert adb.taps, "没点任何东西"
    x, y = adb.taps[0]
    assert x < ui.width * 0.5, f"点到了右侧（确认停止那一半）: {(x, y)}"
    assert abs(x - round(0.23 * ui.width)) <= 2, adb.taps


def test_claim_returns_stopped_when_stop_dialog_shows():
    ui, adb = _ui()
    ui._sync_to_list = lambda tries=3: "list"
    ui._card_button = lambda slot: "expedite"
    ui._wait_expedite_dialog = lambda timeout=3.0: "stopped"
    status = ui.claim_card_fast(2)
    assert status == "stopped", status


# ---------------- 6) 寻访页必须认出来（且不会被干员名当成出货卡） ----------------
def test_screen_kind_detects_headhunt_page():
    ui, _ = _ui()

    def _rec(node, img=None):
        x1 = node.get("x1", 0)
        if x1 >= 0.55:                       # 底栏「寻访一次 / 寻访十次」
            return "寻访十次"
        if x1 <= 0.10:                       # 横幅「概率提升」
            return "以上干员获得概率提升"
        return ""

    ui._rec = _rec
    ui._dialog_text = lambda img=None, deep=False: ""
    ui.on_result_card = lambda: True         # 寻访页有干员名，极易误判成出货卡
    ui.on_lobby = lambda: False
    ui.on_base_screen = lambda: False
    assert ui.screen_kind() == "headhunt"


# ---------------- 7) 卡片定位离配置太远就丢弃 ----------------
def test_locate_cards_drops_bad_quadrant():
    from core.ocr import OcrText
    ui, _ = _ui()

    def box(cx, cy, half=80):
        return [[cx - half, cy - 12], [cx + half, cy - 12],
                [cx + half, cy + 12], [cx - half, cy + 12]]

    # 实测故障帧：1 号位文字被算成 (279,464) 却落进 3 号格
    ui.ocr = type("O", (), {"recognize": lambda self, img: [
        OcrText("开始招募干员", box(279, 464), 0.99)]})()
    assert ui.locate_cards() == {}

    # 正常帧：位置对得上 → 必须保留
    ui.ocr = type("O", (), {"recognize": lambda self, img: [
        OcrText("开始招募干员", box(444, 433), 0.99)]})()
    out = ui.locate_cards()
    assert 1 in out and abs(out[1]["center"][0] - 444) < 2, out


# ---------------- 8) 出货卡识别：奖励栏必须按「单行」读（大块读带读不出来） ----------------
def test_reward_text_uses_single_line_bands():
    """2026-11-08 实机：0.42~0.72 的大块奖励带用 FastOcr 只读出「上」，
    导致 on_result_card 判不出出货卡 → 状态机点中央把卡点穿 → 卡片轮空、没记账。"""
    ui, _ = _ui()
    seen = []

    def _rec(node, img=None):
        seen.append((node["y1"], node["y2"]))
        h = node["y2"] - node["y1"]
        # 只要读带太高（多行）就返回垃圾，单行才返回正常文字
        return "上" if h > 0.10 else "重复获得"

    ui._rec = _rec
    txt = ui._reward_text()
    assert "重复获得" in txt, txt
    assert all((b - a) <= 0.10 for a, b in seen), f"还有多行读带: {seen}"
    ui._reward_text = lambda img=None: "重复获得 +30 资质凭证"
    assert ui.on_result_card() is True


# ---------------- 9) 从「XX的信物」反推干员名 ----------------
def test_name_from_reward_xinwu():
    ui, _ = _ui()
    ui._match_name = lambda t: ("刻刀", 4) if "刻刀" in t else (None, 0)
    name, rar = ui._name_from_reward("重复获得 +30 资质凭证 重复获得 +1 刻刀的信物")
    assert name == "刻刀" and rar == 4, (name, rar)
    assert ui._name_from_reward("重复获得 +30 资质凭证") == (None, 0)


# ---------------- 10) 选词条：必须用检测到的文字中心，并验证是否真的选上 ----------------
def test_select_tags_uses_detected_position_and_retries():
    from core.ocr import OcrText
    ui, adb = _ui()
    # 带检测 OCR 在 tag_area 裁剪图里给出文字框（裁剪图坐标）
    box = [[40, 60], [160, 60], [160, 100], [40, 100]]
    ui.ocr = type("O", (), {"recognize": lambda self, img: [OcrText("召唤", box, 0.99)]})()
    calls = {"n": 0}

    def changed(before, b, min_diff=2.0):
        calls["n"] += 1
        return calls["n"] >= 2          # 第一下判「没高亮」，重试后才算成功

    ui._region_changed = changed
    assert ui.select_tags_fast(["召唤"]) is True
    assert len(adb.taps) == 2, adb.taps
    # 裁剪偏移 (0.26*1600, 0.47*900) + 文字框中心 (100, 80)
    assert adb.taps[0] == (516, 503), adb.taps[0]


def test_select_tags_reports_failure_when_never_highlights():
    from core.ocr import OcrText
    ui, adb = _ui()
    box = [[40, 60], [160, 60], [160, 100], [40, 100]]
    ui.ocr = type("O", (), {"recognize": lambda self, img: [OcrText("削弱", box, 0.99)]})()
    ui._region_changed = lambda *a, **k: False
    ui._tag_hits = {}
    assert ui.select_tags_fast(["削弱"]) is False
    assert len(adb.taps) == 2, adb.taps
