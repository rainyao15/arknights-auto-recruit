"""控制流回归测试（离线，不碰 ADB、不碰真游戏）。

覆盖 2026-11-03 实测踩到的坑 + 面板要求的「批量挂单」打法：

1. 加急后必须先点「聘用候选人」，再到「黑箱动画(只有 SKIP)」，最后才是出货卡；
   缺任何一步都读不到人（11-03 00:09 就是这么失败的）。
2. 出货卡读人要在动画阶段**边点 SKIP 边轮询**，不能一次性 OCR 就放弃。
3. batch 打法必须「4 位先全挂满，再逐个领」—— 顺序错了效率就白搭。

跑法：
    python tests/test_flow.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import paths
from core.config import load_config
from core.logger import setup_logger
from core.runner import Runner

CFG = load_config(paths.resolve_config())
CFG["notify"]["enabled"] = False          # 测试别发真邮件
LOG = setup_logger(os.path.join(tempfile.mkdtemp(), "logs"))


def _runner(slots=4, seed=20260103, on_state=None):
    CFG["dry_run"]["seed"] = seed
    r = Runner(CFG, logger=LOG, dry_run=True, on_state=on_state)
    r.prepare()
    return r


def test_batch_places_all_then_claims():
    """batch 打法：4 个 place 必须全部排在第一个 claim 之前。"""
    events = []
    r = _runner(on_state=lambda st: events.append(st))
    done = r.run_cycle([1, 2, 3, 4], flow_mode="batch")
    phases = [e.get("phase") for e in events if e.get("phase") in ("place", "claim")]
    assert phases.count("place") == 4, phases
    assert "claim" in phases, phases
    first_claim = phases.index("claim")
    assert phases[:first_claim] == ["place"] * 4, f"顺序不对: {phases}"
    assert done == 4, f"应领到 4 位，实际 {done}"
    assert r.sim.state == "list", r.sim.state


def test_locked_slots_are_skipped():
    """没解锁的槽位（人力办公室等级不够）整局都不该再点。

    实机现象：点了 3/4 号位没反应 → 以前每轮都要重走一遍慢速 OCR，
    用户看到的就是「招募完一个后一直对着空气点三四下，过一会才招第二个」。
    """
    events = []
    r = _runner(on_state=lambda st: events.append(st))
    r.ui.locked_slots = {3, 4}
    r.cycle = 1
    done = r.run_cycle([1, 2, 3, 4], flow_mode="batch")
    placed = {e["slot"] for e in events if e.get("phase") == "place"}
    assert placed <= {1, 2}, f"锁住的格子不该再点: {placed}"
    assert done == 2, f"只该领到 2 位，实际 {done}"


def test_batch_reads_every_operator():
    """一轮 4 位，出货干员名必须全读到（不能出现空名字）。"""
    r = _runner()
    r.run_cycle([1, 2, 3, 4], flow_mode="batch")
    names = [rec["name"] for rec in r.stats.session]
    assert len(names) == 4, names
    assert all(names), f"出货名不能为空：{names}"
    assert all(rec["rarity"] >= 3 for rec in r.stats.session), r.stats.session


def test_standard_mode_still_works():
    """标准模式（一槽到底）不能因为改了 batch 而坏掉。"""
    r = _runner()
    done = r.run_cycle([1, 2], flow_mode="standard")
    assert done == 2, done
    assert all(rec["name"] for rec in r.stats.session), r.stats.session


def test_read_result_after_black_box_animation():
    """动画屏（只有 SKIP + 部门 logo）→ 点 SKIP → 读出货干员。"""
    r = _runner()
    r.sim.state = "anim"
    r.sim.cur = 0
    r.sim._result = None
    name, rarity = r.ui.read_result(timeout=15)
    assert name, "动画阶段必须能自己点 SKIP 并读到干员名"
    assert rarity >= 3, rarity
    assert r.sim.state == "result", r.sim.state


def test_claim_then_result_state_chain():
    """状态链：claim → anim → result，不能跳步。"""
    r = _runner()
    r.ui.open_slot(1)
    r.sim.state = "claim"
    assert r.ui.claim_candidates() is True, "必须点到「聘用候选人」"
    assert r.sim.state in ("anim", "result"), r.sim.state
    name, rarity = r.ui.read_result(timeout=15)
    assert name and rarity >= 3


if __name__ == "__main__":
    fns = [f for k, f in sorted(globals().items()) if k.startswith("test_") and callable(f)]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n全部 {len(fns)} 个流程测试通过")
