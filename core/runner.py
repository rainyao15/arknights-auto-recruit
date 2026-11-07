"""公招执行器：CLI（main.py）与 GUI（gui.py）共用同一套流程。

两种打法（config.game.flow_mode，或命令行/面板里临时选）：

  batch    —— 你说的那种高效打法：**先把 1~4 号位全部挂上 9 小时（只挂不领），
              四个都挂满之后，再逐个点开加急领取**。
              好处：面板切换次数减半、OCR 读时长的次数从「每槽一轮」变成「每轮四槽」，
              真机上一轮四槽实际耗时从 ~4.5 分钟压到 ~3 分钟。

  standard —— 原来的一槽到底：挂上 → 立刻加急 → 领完 → 再下一个。

对外只有三步：prepare() → run() / stop()。
"""
import os
import threading
import time
import traceback

from . import paths
from .adb_client import AdbClient, AdbError
from .data_loader import DataLoader
from .decision import decide, explain_risky_tags
from .logger import get_logger
from .notifier import Notifier
from .ocr import OcrEngine
from .stats import Stats
from .ui import UI, UIFlowError

log = get_logger()


class Runner:
    def __init__(self, cfg, logger=None, dry_run: bool = False, on_state=None):
        self.cfg = cfg
        self.log = logger or log
        self.dry_run = dry_run
        self.on_state = on_state or (lambda st: None)   # GUI 状态回调
        self.stop_event = threading.Event()

        self.ui = None
        self.data = None
        self.stats = None
        self.notifier = None
        self.sim = None
        self.device = ""
        self.cycle = 0
        self.no_expedite = False     # 加急许可用完 → 转自然招募
        self.current = ""            # 人类可读的当前动作

    # ------------------------------------------------------------------ 基础
    def _emit(self, **kw):
        try:
            self.on_state(dict(kw))
        except Exception:
            pass

    def _wait(self, seconds: float, step: float = 0.2) -> bool:
        """可被打断的 sleep；返回 True 表示收到停止。"""
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_event.is_set():
                return True
            time.sleep(min(step, max(0.0, end - time.time())))
        return self.stop_event.is_set()

    @property
    def stopped(self) -> bool:
        return self.stop_event.is_set()

    # ------------------------------------------------------------------ 自愈
    def health_check(self) -> bool:
        """无人值守体检：ADB 掉线就重连；游戏掉到桌面就重新拉起。"""
        if self.dry_run:
            return True
        try:
            alive = getattr(self.ui.adb, "alive", None)
            if alive is not None and not alive():
                self.log.warning("ADB 没响应 → 重连 %s", self.device or "device")
                self.ui.adb.connect()
                if not self.ui.adb.alive():
                    self.log.error("ADB 重连失败")
                    return False
                self.log.info("ADB 已重连")
        except Exception as e:
            self.log.error("ADB 恢复失败: %s", e)
            return False
        # 前台守卫：游戏被切到后台/崩掉时停在模拟器桌面，此时按坐标点就是在
        # 桌面上乱点（会打开别的 App、把游戏彻底退掉）。2026-11-03 实测。
        try:
            if not self.ui.in_game():
                self.log.warning("游戏不在前台（%s）→ 重新拉起",
                                 getattr(self.ui, "_last_fg", "") or "未知")
                self.ui.launch_game()
                return self.ui.in_game()
        except Exception as e:
            self.log.error("拉起游戏失败: %s", e)
            return False
        return True

    def hard_reset_game(self) -> None:
        """终极手段：强杀游戏进程再拉起（挂机时迷路就靠这个回到干净状态）。"""
        pkg = self.cfg["game"].get("package", "com.hypergryph.arknights")
        self.log.warning("硬重置：force-stop %s 后重新启动", pkg)
        try:
            self.ui.adb._sh("am", "force-stop", pkg)
        except Exception as e:
            self.log.warning("force-stop 失败: %s", e)
        self._wait(3)
        try:
            self.ui.launch_game()
        except Exception as e:
            self.log.error("重启游戏失败: %s", e)
        self._wait(45)                       # 等它起来并进大厅

    def recover_ui(self, reason: str = "") -> None:
        """把界面拉回公招列表：关弹窗 → 进公招 → 还不行就硬重置。"""
        try:
            if reason:
                self.log.info("自愈（%s）", reason)
            if not self.health_check():
                self._wait(10)
                return
            ui = self.ui
            ui._last_img = None
            if ui.fast_ready() and ui.on_base_screen():   # 误入基建先退出来
                ui.leave_base()
            for i in range(2):
                ui._last_img = None
                ui.kill_popups_fast(rounds=2)
                if ui.ensure_list_fast(attempts=2):
                    return
                if ui.fast_ready() and ui.enter_recruit_fast(tries=1):
                    return
                try:
                    ui.ensure_recruit_screen()
                except Exception as e:
                    self.log.warning("第 %d 次进公招失败: %s", i + 1, str(e)[:50])
                if ui.ensure_list_fast(attempts=1):
                    return
            self.hard_reset_game()
            if ui.fast_ready():
                ui.enter_recruit_fast(tries=2)
        except Exception as e:
            self.log.error("自愈过程出错: %s", e)

    # ------------------------------------------------------------------ 准备
    def prepare(self) -> bool:
        """连设备 + 加载数据 + 建 UI。失败抛 AdbError / UIFlowError。"""
        paths.ensure_dirs()
        from .simulator import FakeAdb, FakeOcr, SimScreen

        if self.dry_run:
            seed = self.cfg.get("dry_run", {}).get("seed", 20260101)
            slot_pos = [(s["x"], s["y"]) for s in (self.cfg["ui"].get("slots") or [])]
            self.sim = SimScreen(seed=seed,
                                 slots=self.cfg["game"].get("max_slots", 4),
                                 slot_pos=slot_pos,
                                 ui=dict(self.cfg["ui"]),
                                 time_step_min=self.cfg["ui"].get("time_step_min", 60))
            adb, ocr = FakeAdb(self.sim), FakeOcr(self.sim)
        else:
            adb, ocr = AdbClient(self.cfg), OcrEngine(self.cfg)

        self.device = adb.connect()
        self.data = DataLoader(self.cfg).load()
        self.ui = UI(adb, ocr, self.cfg, self.data)

        if self.dry_run:
            self.cfg["notify"]["enabled"] = False
            hist = paths.HISTORY_SIM_PATH
        else:
            hist = paths.HISTORY_PATH
        self.stats = Stats(self.cfg, history_path=hist)
        self.notifier = Notifier(self.cfg)

        self.log.info("设备就绪: %s（%dx%d）", self.device, self.ui.width, self.ui.height)
        if not self.data.operators:
            self.log.warning("干员数据为空，决策只用词条规则")
        self.log.info(self.stats.total_summary())
        self._emit(device=self.device, width=self.ui.width, height=self.ui.height,
                   total=self.stats.total_summary(), phase="ready")
        return True

    # ------------------------------------------------------------------ 单槽动作
    def place_slot(self, slot: int) -> dict:
        """把第 slot 个公招位挂上（读词条 → 决策 → 9h → 开始招募）。不领。"""
        ui = self.ui
        mode = self.cfg["decision"].get("mode", "basic")
        fallback = self.cfg["decision"].get("fallback", "none")
        hours = self.cfg["game"].get("recruit_time_hours", 9)

        self.current = f"挂单 · 第 {slot} 位"
        self._emit(phase="place", slot=slot, text=self.current)
        self.log.info("【挂单】第 %d 位", slot)

        t0 = time.time()
        fast = ui.fast_ready()

        if fast:
            if not ui.open_slot_fast(slot):
                self.log.warning("第 %d 格没敢点（不在公招列表页）→ 自愈", slot)
                self.recover_ui("open_slot 前置校验失败")
                return {}
            state, cur = ui.panel_state_fast()
            if state == "other":                 # 可能点早了（面板有 0.3~0.8s 的弹出动画）
                time.sleep(0.45)
                ui._last_img = None
                state, cur = ui.panel_state_fast()
            if state == "other" and ui.screen_kind() == "list":
                # 真没打开才补点；面板已经开了再点会误选到词条
                ui.open_slot_fast(slot)
                time.sleep(0.45)
                ui._last_img = None
                state, cur = ui.panel_state_fast()
            if state == "placing":
                budget = ui.budget_left()
                if budget == 0:
                    self.log.warning("招募许可用完（预算 0/1）→ 收工")
                    self.notifier.notify("公招：招募许可用完", "招募预算 0/1")
                    ui.cancel_panel()
                    self.stop_event.set()
                    return {}
                tags = ui.read_tags_fast()
                if len(tags) < 2:                    # 网格没标准 → 退回慢速读词条
                    log.debug("极速词条只读到 %s，退回慢速", tags)
                    tags = ui.read_tags()
                if not tags:
                    log.warning("第 %d 位没读到词条 → 跳过", slot)
                    ui.cancel_panel()
                    return {}
                risky = explain_risky_tags(tags, self.data.operators)
                if risky:
                    self.log.info("词条 %s 会降星，决策避开", "、".join(risky))
                combo, reason = decide(tags, mode=mode, operators=self.data.operators,
                                       fallback=fallback)
                self.log.info("决策: %s —— %s",
                              "+".join(combo) if combo else "（不选词条）", reason)
                ui.select_tags_fast(combo)
                ui.set_time_fast(hours)
                if not ui.confirm_fast(verify=False):
                    ui.kill_popups_fast(rounds=2)
                    self.log.warning("第 %d 位招募没提交成功 → 本轮不领它", slot)
                    ui.save_shot(f"slot{slot}_confirm_fail")
                    raise UIFlowError("挂单没提交成功")
                # 实机：✓ 之后游戏**不关面板**，而是切成「招募中」视图；
                # 这里顺手把它关掉，否则下一格会误判「面板还开着」→ 多一次自愈。
                kind = ui.finish_panel()
                if kind != "list":
                    self.log.info("挂单后界面停在 %s → 轻量同步回列表", kind)
                    ui.ensure_list_fast(attempts=2)
                self.log.info("【挂单】第 %d 位完成（%dh，用时 %.1fs）",
                              slot, hours, time.time() - t0)
                return {"slot": slot, "combo": combo, "reason": reason, "tags": tags}
            if state == "dialog":
                ui.kill_popups_fast(rounds=2)
                ui.open_slot_fast(slot)
                state, cur = ui.panel_state_fast()
            if state == "running":
                self.log.info("第 %d 位正在招募中 → 留到领取阶段", slot)
                ui.cancel_panel()
                return {"slot": slot, "running": True}
            self.log.warning("第 %d 位极速判定为 %s → 退回慢速流程", slot, state)

        ui.open_slot(slot)
        self._wait(0.5)

        state = ui.panel_state()
        if state == "running":
            self.log.info("第 %d 位正在招募中 → 挂单跳过，留到领取阶段", slot)
            return {"slot": slot, "running": True}
        if state != "placing":
            self.log.warning("第 %d 位词条面板没打开（%s）→ 跳过", slot, state)
            ui.cancel_panel()
            return {}

        tags = ui.read_tags()
        if not tags:
            self.log.warning("第 %d 位没读到词条（空位/已占用/未解锁）→ 跳过", slot)
            ui.save_shot(f"slot{slot}_no_tags")
            ui.cancel_panel()
            return {}

        risky = explain_risky_tags(tags, self.data.operators)
        if risky:
            self.log.info("本次词条里 %s 会降星，决策会避开", "、".join(risky))

        combo, reason = decide(tags, mode=mode, operators=self.data.operators,
                               fallback=fallback)
        self.log.info("决策: %s —— %s",
                      "+".join(combo) if combo else "（不选词条）", reason)

        ui.select_tags(combo)
        try:
            ui.set_time(hours)
        except UIFlowError as e:
            # 设备抖动时面板可能没真打开（读时长会读到列表页的剩余时间）→ 重开一次
            self.log.warning("设时长失败（%s）→ 重开第 %d 位再试一次", e, slot)
            ui.save_shot(f"slot{slot}_time_retry")
            ui.cancel_panel()
            ui.open_slot(slot)
            self._wait(0.6)
            if not ui.read_tags():
                raise
            ui.select_tags(combo)
            ui.set_time(hours)
        ui.confirm()
        self.log.info("【挂单】第 %d 位完成（%dh，词条 %s）", slot, hours,
                      "+".join(combo) if combo else "无")
        return {"slot": slot, "combo": combo, "reason": reason, "tags": tags}

    def claim_slot(self, slot: int, combo=None, reason: str = "") -> dict:
        """对已挂好的第 slot 位用加急许可领取并读出货。"""
        ui = self.ui
        self.current = f"领取 · 第 {slot} 位"
        self._emit(phase="claim", slot=slot, text=self.current)
        self.log.info("【领取】第 %d 位", slot)

        t0 = time.time()
        if ui.fast_ready() and not self.dry_run:
            if self.no_expedite:
                # 自然招募模式：槽位只有到时间才会出现「聘用候选人」
                if not ui.claim_candidates():
                    self.log.info("第 %d 位还没招募完 → 下一轮再来", slot)
                    return {}
                ui.wait_result(timeout=6)
                name, rarity = ui.read_name_fast()
                self.stats.record(slot, name or "", rarity or 0, combo or [], reason or "")
                ui.close_result_fast()
                return {"slot": slot, "name": name or "", "rarity": rarity or 0}

            status = ui.claim_card_fast(slot)
            if status == "notlist":
                self.log.warning("领取前发现不在公招列表 → 轻量同步一次界面")
                ui.ensure_list_fast(attempts=1)
                return {}
            if status in ("empty", "unknown"):
                # 领取失败**不做全量自愈**：本地只收手，下一格/下一轮再来。
                # （2026-11-03 就是这里触发自愈，连点返回把游戏退回了桌面）
                self.log.warning("第 %d 位这次没领成（%s）→ 跳过这一格", slot, status)
                try:
                    ui.ensure_list_fast(attempts=1)
                except Exception:
                    pass
                return {}
            if status == "no_expedite":
                self.no_expedite = True
                self.log.warning("加急许可用完 → 后面改自然招募（每轮等 9h）")
                self.notifier.notify("公招：加急用完", "已转自然招募模式")
                return {}
            name, rarity = ui.read_name_fast()
            if getattr(ui, "claim_diag", "") == "no_expedite":
                self.no_expedite = True
                self.log.warning("加急许可用完 → 后面改自然招募（每轮等 9h）")
                self.notifier.notify("公招：加急用完", "已转自然招募模式")
                return {}
            if not name and getattr(ui, "claim_diag", "") == "dialog":
                # ✓ 那一拍没落上，弹窗还挂着 → 关掉重来一次（只重试一次）
                self.log.info("第 %d 位第一次没领成，关弹窗后重试一次", slot)
                ui.dismiss_dialog()
                ui.claim_card_fast(slot)
                name, rarity = ui.read_name_fast()
            if name:
                self.log.info("第 %d 位出货: %d★ %s（用时 %.1fs）",
                              slot, rarity, name, time.time() - t0)
                self.notifier.maybe_notify_result(name, rarity, combo or [], reason or "")
                self.stats.record(slot, name, rarity or 0, combo or [], reason or "")
            else:
                # 没读到名字就别记账（以前会记一条空记录，把「本次 N 次」虚高）
                self.log.warning("第 %d 位出货识别失败（截图见 logs/，不记账）", slot)
                ui.save_shot(f"slot{slot}_result")
            ui.close_result_fast()
            if self.stats.session:
                self._emit(phase="claim", slot=slot,
                           record={"name": name or "", "rarity": rarity or 0},
                           session=self.stats.session_summary())
            return {"slot": slot, "name": name or "", "rarity": rarity or 0}

        if not ui.use_expedite(slot):
            how = self.cfg["game"].get("on_expedite_exhausted", "stop")
            self.log.warning("第 %d 位加急失败（加急许可可能用完了）", slot)
            if how == "stop":
                raise UIFlowError("加急许可耗尽 → 停止")
            self.log.info("按配置挂 %dh 自然招募，跳过领取",
                          self.cfg["game"].get("recruit_time_hours", 9))
            return {}

        if not ui.claim_candidates():
            self.log.warning("第 %d 位没出现「聘用候选人」", slot)
            ui.save_shot(f"slot{slot}_no_claim")

        name, rarity = ui.read_result()
        if name:
            self.log.info("第 %d 位出货: %d★ %s", slot, rarity, name)
            self.notifier.maybe_notify_result(name, rarity, combo or [], reason or "")
        else:
            self.log.warning("第 %d 位出货识别失败（截图见 logs/）", slot)
            ui.save_shot(f"slot{slot}_result")

        self.stats.record(slot, name or "", rarity or 0, combo or [], reason or "")
        ui.close_result()

        if self.stats.session:
            self._emit(phase="claim", slot=slot, record={
                "name": name or "", "rarity": rarity or 0},
                session=self.stats.session_summary())
        return {"slot": slot, "name": name or "", "rarity": rarity or 0}

    # ------------------------------------------------------------------ 一轮
    def run_cycle(self, slots, flow_mode: str = "batch") -> int:
        """跑一轮。batch=先全挂满再逐个领；standard=一槽到底。返回领到几个。"""
        ui = self.ui
        self.log.info("=" * 46)
        self.log.info("第 %d 轮开始 · 模式 %s · 槽位 %s", self.cycle,
                      "批量挂单" if flow_mode == "batch" else "标准",
                      ",".join(str(s) for s in slots))
        try:
            # 快速路径：先分类当前页面，只做对应动作；慢速路径保留原来的 OCR 流程
            if ui.fast_ready() and ui.ensure_list_fast(attempts=2):
                pass
            else:
                ui.ensure_recruit_screen()
        except UIFlowError as e:
            self.log.warning("本轮进公招页失败: %s → 自愈", e)
            self.recover_ui("run_cycle 进页失败")

        if ui.fast_ready():
            ui.kill_popups_fast()
            links = ui.links_left()
            ui._time_checked = False          # 每轮重新校验一次时长
            if links is not None:
                # 注意：联络次数是「人脉联络/刷新标签」的额度，跟能不能招募**无关**
                # （实测 0/3 照样能用招募许可开位），所以只记日志、不参与判断
                self.log.info("人脉联络次数: %d/3（不影响招募，仅记录）", links)
            ui.ensure_list_fast()

        done = 0
        fail_streak = 0
        if flow_mode == "batch":
            # 1) 先把所有空位挂满；正在招募的位记下来，等会儿一起领
            placed, claim_order = {}, []
            for slot in slots:
                if self.stopped:
                    break
                try:
                    r = self.place_slot(slot)
                    if r:
                        placed[slot] = r
                        claim_order.append(slot)
                except (UIFlowError, AdbError) as e:
                    fail_streak += 1
                    self.log.error("第 %d 位挂单失败: %s（连续 %d 次）", slot, e, fail_streak)
                    ui.save_shot(f"c{self.cycle}_slot{slot}_place_err")
                    try:
                        ui.cancel_panel()
                    except Exception:
                        pass
                    if fail_streak >= 3:
                        self.log.error("连续 3 格挂单失败 → 收工（可能没招募许可/游戏不在公招页）")
                        self.notifier.notify("公招异常停止", f"连续 {fail_streak} 格挂单失败")
                        self.stop_event.set()
                        break
                self._wait(0.4)

            if len(claim_order) < len(slots):
                self.log.info("本轮挂上 %d/%d 位（其余空位/异常已跳过）",
                              len(claim_order), len(slots))

            # 2) 再逐个加急领取（含刚挂上的 + 本来就在跑的）
            if claim_order and ui.fast_ready():
                ui.ensure_list_fast(attempts=2)      # 领之前先保证在列表页
            for slot in claim_order:
                if self.stopped:
                    break
                info = placed.get(slot) or {}
                try:
                    got = self.claim_slot(slot, info.get("combo"), info.get("reason"))
                    if got:
                        done += 1
                except (UIFlowError, AdbError) as e:
                    self.log.error("第 %d 位领取中断: %s", slot, e)
                    ui.save_shot(f"c{self.cycle}_slot{slot}_claim_err")
                    if "加急许可耗尽" in str(e):
                        self.notifier.notify("加急许可耗尽，脚本停止",
                                             self.stats.session_summary())
                        self.stop_event.set()
                        break
                self._wait(0.4)
        else:
            for slot in slots:
                if self.stopped:
                    break
                try:
                    info = self.place_slot(slot)
                    if info and self.claim_slot(slot, info.get("combo"), info.get("reason")):
                        done += 1
                except (UIFlowError, AdbError) as e:
                    self.log.error("第 %d 位失败: %s", slot, e)
                    ui.save_shot(f"c{self.cycle}_slot{slot}_err")
                    if "加急许可耗尽" in str(e):
                        break
                self._wait(0.4)
        return done

    # ------------------------------------------------------------------ 主循环
    def run(self, flow_mode: str = "batch", slots=None, cycles: int = 0,
            interval_min: float = None):
        """阻塞跑，直到 stop() 或跑满 cycles 轮。"""
        self.stop_event.clear()
        slots = slots or list(range(1, int(self.cfg["game"].get("max_slots", 4)) + 1))
        if interval_min is None:
            interval_min = float(self.cfg["game"].get("loop_interval_min", 5))

        if self.ui is None and not self.prepare():
            return

        if not self.dry_run:
            try:
                self.ui.launch_game()          # 内部含 wait_ready：加载页只轻点不按返回
            except AdbError as e:
                self.log.warning("启动游戏失败，直接尝试进公招: %s", e)
        try:
            if self.ui.fast_ready() and self.ui.ensure_list_fast(attempts=3):
                pass
            else:
                self.ui.ensure_recruit_screen()
        except UIFlowError as e:
            self.log.error("进公招页失败: %s → 自愈后继续", e)
            self._emit(phase="error", text=str(e))
            self.recover_ui("进公招页失败")

        self.cycle = 0
        while not self.stopped:
            self.cycle += 1
            t_cycle = time.time()
            try:
                if not self.health_check():
                    self.log.warning("设备不可用 → 等 15s 再试")
                    self._wait(15)
                    continue
                n = self.run_cycle(slots, flow_mode)
                self._fail_cycles = 0
            except (UIFlowError, AdbError) as e:
                self.log.error("第 %d 轮中断: %s", self.cycle, e)
                n = 0
                self._fail_cycles = getattr(self, "_fail_cycles", 0) + 1
                self.recover_ui(str(e)[:60])
                if self._fail_cycles >= 3:
                    self.log.warning("连续 %d 轮异常 → 重开游戏", self._fail_cycles)
                    try:
                        self.ui.launch_game()
                    except Exception as e2:
                        self.log.error("重开游戏失败: %s", e2)
                    self._fail_cycles = 0
            except Exception:
                self.log.error("第 %d 轮未知异常: %s", self.cycle,
                               traceback.format_exc())
                n = 0
                self._fail_cycles = getattr(self, "_fail_cycles", 0) + 1
                self.recover_ui("未知异常")
            self.log.debug("本轮耗时 %.1fs", time.time() - t_cycle)
            self.log.info("第 %d 轮完成: %d 位", self.cycle, n)
            self.log.info(self.stats.session_summary())
            self._emit(phase="cycle_done", cycle=self.cycle, done=n,
                       session=self.stats.session_summary())

            if self.dry_run and self.sim:
                self.sim.reset_done_slots()

            if cycles and self.cycle >= cycles:
                self.log.info("已跑满 %d 轮", cycles)
                break
            if self.stopped:
                break
            wait_min = interval_min
            if self.no_expedite:
                wait_min = max(wait_min, float(self.cfg["game"].get(
                    "natural_wait_min", 540)))
                self.log.info("自然招募模式：本轮等 %d 分钟（9h）后再收", int(wait_min))
            if wait_min <= 0:
                continue
            self.log.info("等待 %d 分钟后进入下一轮（点「停止」可随时打断）",
                          int(wait_min))
            self.current = f"等待下一轮（{int(wait_min)} 分钟）"
            self._emit(phase="sleep", text=self.current)
            if self._wait(wait_min * 60):
                break

        if self.ui and self.ui.fast_ready():   # 收工前把面板/弹窗清干净
            self.ui.kill_popups_fast(rounds=2)
            self.ui.ensure_list_fast()
        self.log.info("=" * 46)
        self.log.info(self.stats.session_summary())
        self.log.info(self.stats.total_summary())
        best = self.stats.best_of_session()
        if best:
            self.log.info("本次最高: %s", best)
        self._emit(phase="stopped", session=self.stats.session_summary(),
                   total=self.stats.total_summary(), best=best)
        return self.stats.session

    def stop(self):
        self.stop_event.set()
        self.log.info("收到停止请求，当前动作结束后退出…")
