"""核心逻辑单元测试（决策引擎 / 词条识别 / 数据 / 统计 / 规则校验）。

运行：
    python tests/test_core.py          # 不需要 pytest
    python -m pytest tests -q          # 有 pytest 也行
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core.config import load_config
from core.data_loader import DataLoader
from core.decision import (PLAIN_MAX_RARITY, SPECIAL_5, SPECIAL_6,
                           best_safe_tag, combo_max_rarity, decide,
                           describe_lock, explain_risky_tags, guarantee_best,
                           lock_best, match_operators, tag_max_rarity)
from core.stats import Stats
from core.tag_recognizer import extract_tags, match_tag, similarity

_CFG = load_config("config.yaml")
_DATA = DataLoader(_CFG).load()
OPS = _DATA.operators


# ---------------- 决策：组合保底（召唤这种） ----------------
def test_decide_picks_guarantee_combo():
    """「召唤」池里没选资深只剩 4★（豆苗）→ 必须点它，保底 4★。"""
    tags = ["召唤", "群攻", "近战位", "输出", "生存"]
    combo, reason = decide(tags, operators=OPS)
    assert "召唤" in combo, combo
    assert "保底" in reason and "4★" in reason, reason


def test_guarantee_best_finds_combo():
    """没有单词条保底时，组合（输出+群攻）也要找出来。"""
    tags = ["狙击干员", "远程位", "输出", "群攻", "减速"]
    g = guarantee_best(tags, OPS)
    assert g is not None, "这组词条应该存在 4★ 保底组合"
    combo, lo, hi, size = g
    assert lo >= 4 and size >= 1, g
    sel, reason = decide(tags, operators=OPS)
    assert sel, "有保底组合就必须选，不能空选"
    assert "保底" in reason


def test_guarantee_avoids_downgrade_only_tags():
    """只剩降星词条时不硬选，仍然空选。"""
    tags = ["新手", "元素", "支援机械", "近战位", "输出"]
    combo, _ = decide(tags, operators=OPS)
    assert combo == [], combo
    assert guarantee_best(tags, OPS) is None


# ---------------- 决策：保底 ----------------
def test_decide_special_6():
    tags = ["狙击干员", "高级资深干员", "输出", "远程位", "女性干员"]
    combo, reason = decide(tags, operators=OPS)
    assert combo == [SPECIAL_6], combo
    assert "6★" in reason


def test_decide_special_5():
    tags = ["近卫干员", "输出", "生存", "资深干员", "近战位"]
    combo, reason = decide(tags, operators=OPS)
    assert combo == [SPECIAL_5], combo
    assert "5★" in reason


def test_decide_priority_6_over_5():
    tags = ["高级资深干员", "资深干员", "近卫干员", "输出", "近战位"]
    combo, _ = decide(tags, operators=OPS)
    assert combo == [SPECIAL_6], combo


# ---------------- 决策：无特殊词条 ----------------
def test_decide_no_special_selects_nothing():
    """核心修正：无特殊词条时不选任何词条（选普通词条只降星）。"""
    tags = ["近卫干员", "输出", "生存", "近战位", "男性干员"]
    combo, reason = decide(tags, operators=OPS, fallback="none")
    assert combo == [], combo
    assert "不选" in reason


def test_decide_avoids_downgrade_tags():
    """新手/元素/支援机械 一旦被选中会把收益锁死到 1★~2★。"""
    tags = ["新手", "元素", "支援机械", "近战位", "输出"]
    risky = explain_risky_tags(tags, OPS)
    assert set(risky) == {"新手", "元素", "支援机械"}, risky
    combo, _ = decide(tags, operators=OPS, fallback="none")
    assert combo == []
    combo2, _ = decide(tags, operators=OPS, fallback="best_tag")
    assert combo2 == ["输出"], combo2   # best_tag 也不会选降星词条


def test_best_safe_tag_keeps_max_rarity():
    tags = ["新手", "治疗", "远程位", "元素", "输出"]
    best = best_safe_tag(tags, OPS)
    assert best in {"治疗", "远程位", "输出"}, best
    assert tag_max_rarity(best, OPS) == PLAIN_MAX_RARITY


def test_plain_tag_caps_at_4():
    """5★/6★ 都带资深词条，所以普通词条的实际可达上限是 4★。"""
    assert tag_max_rarity("输出", OPS) == 4
    assert tag_max_rarity("新手", OPS) == 2
    assert tag_max_rarity(SPECIAL_5, OPS) == 5
    assert tag_max_rarity(SPECIAL_6, OPS) == 6
    # 性别词条在 MAA 数据里没有标注，不能误判成空池
    assert tag_max_rarity("女性干员", OPS) == PLAIN_MAX_RARITY


# ---------------- 池子计算 ----------------
def test_match_operators_respects_special_gate():
    """没选资深词条时，5★/6★ 干员不可能被抽到。"""
    plain = match_operators(["近卫干员", "输出", "生存"], OPS)
    assert plain, "近卫+输出+生存 应该有匹配"
    assert all(op["rarity"] <= 4 for op in plain), [o["name"] for o in plain]

    with_senior = match_operators(["近卫干员", "输出", "生存", SPECIAL_5], OPS)
    assert any(op["rarity"] == 5 for op in with_senior)

    with_top = match_operators(["近卫干员", SPECIAL_6], OPS)
    assert any(op["rarity"] == 6 for op in with_top)


def test_combo_max_rarity():
    assert combo_max_rarity([], OPS) == PLAIN_MAX_RARITY
    assert combo_max_rarity([SPECIAL_6], OPS) == 6
    assert combo_max_rarity([SPECIAL_5], OPS) == 5
    assert combo_max_rarity(["元素"], OPS) <= 2


def test_lock_best_never_downgrades():
    tags = ["近卫干员", "输出", "生存", "近战位", "男性干员"]
    combo = lock_best(tags, OPS)
    assert combo, "应该能锁出组合"
    assert combo_max_rarity(combo, OPS) == 4
    assert describe_lock(combo, OPS) != "无匹配"


# ---------------- 数据完整性 ----------------
def test_data_stats():
    assert len(OPS) > 100
    assert len(_DATA.tags) == 31
    for op in OPS:
        if op["rarity"] == 6:
            assert SPECIAL_6 in op["tags"], op
        if op["rarity"] == 5:
            assert SPECIAL_5 in op["tags"], op


# ---------------- 词条识别 ----------------
def test_tag_match():
    assert match_tag("高级资深干员") == "高级资深干员"
    assert match_tag("资深干员") == "资深干员"
    assert match_tag("狙击干员") == "狙击干员"
    assert match_tag("随缘乱码文本") is None
    assert similarity("高 级 资 深 干 员", SPECIAL_6) > 0.9


def test_extract_tags_dedup():
    texts = ["高级资深干员", "高级资深干员", "狙击干员", "输出", "远程位"]
    tags = extract_tags(texts)
    assert tags.count("高级资深干员") == 1
    assert "狙击干员" in tags


# ---------------- 统计 ----------------
def test_stats_record_and_summary(tmp=None):
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "h.jsonl")
    st = Stats(_CFG, history_path=path)
    base = st.tickets(st.totals)
    st.record(1, "玫兰莎", 3, ["输出"], "test")
    st.record(2, "宴", 4, [], "test")
    st.record(3, "", 0, [], "加急不可用")
    assert st.totals.get(3) == 1 and st.totals.get(4) == 1
    assert st.tickets(st.totals) - base == 2
    s = st.session_summary()
    assert "4★×1" in s and "3★×1" in s
    assert "宴" == st.best_of_session().split()[-1]
    # 重新加载能读回历史
    st2 = Stats(_CFG, history_path=path)
    assert st2.totals.get(4) == 1


# ---------------- 出货页「快进」推进：安全闸 ----------------
def test_advance_result_never_taps_on_list():
    """开干员包页的兜底是「点屏幕中央」，所以列表/面板/大厅必须一下都不点。

    2026-11-03 的教训：在列表页点中央正好选中某个公招格，后面几格全乱。
    """
    from core.simulator import FakeAdb, FakeOcr, SimScreen
    from core.ui import UI

    sim = SimScreen(seed=1)
    adb = FakeAdb(sim)
    ui = UI(adb, FakeOcr(sim), _CFG, _DATA)
    ui.dry_run = False                      # 强制走真实分支（模拟器对象没有像素）

    for kind in ("list", "panel", "lobby", "base"):
        ui.screen_kind = lambda k=kind: k
        n = len(adb.taps)
        assert ui.advance_result() == "none", kind
        assert len(adb.taps) == n, f"{kind} 上不该点，却点了"

    ui.screen_kind = lambda: "other"
    ui._last_img = None
    n = len(adb.taps)
    assert ui.advance_result() == "center"
    assert len(adb.taps) == n + 1, "非列表页应该点一次快进"
    x, y = adb.taps[-1]
    assert (round(x / ui.width, 2), round(y / ui.height, 2)) == (0.5, 0.5)


def test_card_button_uses_real_center_plus_offset():
    """按钮位置 = 实际卡片中心 + 配置里的相对偏移（PC 布局和模拟器不同也必须点得准）。"""
    from core.simulator import FakeAdb, FakeOcr, SimScreen
    from core.ui import UI

    sim = SimScreen(seed=11)
    adb = FakeAdb(sim)
    ui = UI(adb, FakeOcr(sim), _CFG, _DATA)
    ui.card_map = {3: {"center": (444.0, 732.0), "button": None}}

    slot = _CFG["ui"]["slots"][2]          # 第 3 格
    exp = _CFG["ui"]["fast"]["cards"][2]["expedite"]
    ox = (exp["x"] - slot["x"]) * ui.width
    oy = (exp["y"] - slot["y"]) * ui.height

    bx, by = ui.card_button(3)
    assert abs(bx - (444 + ox)) < 1.0, (bx, 444 + ox)
    assert abs(by - (732 + oy)) < 1.0, (by, 732 + oy)

    # OCR 直接读到按钮文字时，用它给的精确位置
    ui.card_map[3]["button"] = (700.0, 800.0)
    assert ui.card_button(3) == (700.0, 800.0)

    # 没定位到卡片 → 回退到配置比例
    ui.card_map = {}
    assert ui.card_button(3) == (exp["x"] * ui.width, exp["y"] * ui.height)


def test_result_close_taps_capped_at_one():
    """关卡（点左下空白）整个领取只允许 1 下，之后必须返回 False。"""
    from core.simulator import FakeAdb, FakeOcr, SimScreen
    from core.ui import UI

    sim = SimScreen(seed=3)
    adb = FakeAdb(sim)
    ui = UI(adb, FakeOcr(sim), _CFG, _DATA)
    ui.dry_run = False
    ui.cfg["ui"]["result_close_taps"] = 1

    n = len(adb.taps)
    assert ui.tap_result_close() is True
    assert len(adb.taps) == n + 1
    assert ui.tap_result_close() is False, "第二下必须被挡住"
    assert len(adb.taps) == n + 1, "第二下不该真的点出去"

    ui._close_taps = 0                       # 新一次领取 → 重新计数
    assert ui.tap_result_close() is True


def test_skip_result_pages_no_blind_retap():
    """点了没反应时只许点一下，不许在列表上空点好几下。"""
    from core.simulator import FakeAdb, FakeOcr, SimScreen
    from core.ui import UI

    sim = SimScreen(seed=7)
    adb = FakeAdb(sim)
    ui = UI(adb, FakeOcr(sim), _CFG, _DATA)
    ui.dry_run = False
    ui.screen_kind = lambda: "other"          # 永远认不出界面
    ui.on_result_card = lambda: False
    ui._frame = lambda: None
    ui._wait_change = lambda *a, **k: False   # 模拟「点了界面纹丝不动」

    n = len(adb.taps)
    assert ui.skip_result_pages(max_taps=4) is False
    assert len(adb.taps) == n + 1, f"应该只点 1 下，实际点了 {len(adb.taps) - n} 下"

    ui._wait_change = lambda *a, **k: True    # 界面有反应 → 允许继续推
    ui.cfg["ui"]["result_pages"] = {"max_taps": 3, "timeout": 3.5}
    n = len(adb.taps)
    ui.skip_result_pages(max_taps=3)
    assert len(adb.taps) == n + 3, f"有反应时应点满 3 下，实际 {len(adb.taps) - n}"


if __name__ == "__main__":
    fns = [f for k, f in sorted(globals().items()) if k.startswith("test_") and callable(f)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 个测试通过")


# ---------------- 词条识别：两字词条容错（2026-11-08 新增） ----------------
def test_match_tag_tolerates_one_char_typo_on_short_tags():
    """两字词条 OCR 错一个字相似度只有 0.5，旧逻辑直接丢弃 → 削弱/输出 会漏。"""
    assert match_tag("削弱") == "削弱"
    assert match_tag("削羽") == "削弱"          # 相似度 0.50 + 唯一最优
    assert match_tag("输岀") == "输出"
    assert match_tag("群玫") == "群攻"
    assert match_tag("快速复话") == "快速复活"   # 三字以上本来就够 0.75


def test_match_tag_rejects_ambiguous_short_noise():
    """等距歧义（输出/生存 都是 0.5）不许瞎猜。"""
    assert match_tag("生出") is None
    assert match_tag("一") is None


def test_recruit_data_integrity():
    """公招数据体检：词条白名单齐全、星级合法、资深池只含对应星级。"""
    assert len(_DATA.tags) >= 31
    for op in OPS:
        assert 1 <= op["rarity"] <= 6, op
        assert op["tags"], op
    six = {op["rarity"] for op in OPS if "高级资深干员" in op["tags"]}
    five = {op["rarity"] for op in OPS if "资深干员" in op["tags"]}
    assert six == {6}, six
    assert five == {5}, five
