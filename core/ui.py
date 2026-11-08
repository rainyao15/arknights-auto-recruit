"""UI 自动化：OCR 驱动（按文字找按钮）+ 坐标兜底。

设计原则：
1. **优先按文字点**（截图 → OCR → 找关键词 → 点中心），对分辨率/皮肤/微调免疫；
2. 坐标只用于「无文字语义」的位置（4 个公招位格子、时间 +/- 按钮）；
3. 每一步都能截图存档，失败原因可回溯。
"""
import os
import re
import time
from datetime import datetime

from .data_loader import RecruitData
from .logger import get_logger
from .ocr import FastOcr
from .tag_recognizer import extract_tags, match_tag, similarity

log = get_logger()

from . import paths

ROOT = paths.ROOT
SHOT_DIR = paths.LOG_DIR

# 关闭各种弹窗（登录奖励 / 公告 / 活动 / 更新提示）
CLOSE_KEYWORDS = ["点击屏幕", "确定", "确认", "知道了", "关闭", "跳过", "×", "X"]

# 公招入口：主界面右侧图标在部分 UI/皮肤下显示英文（RECRUITMENT），只认中文会直接找不到入口
RECRUIT_ENTRY_KW = ["公开招募", "RECRUITMENT", "Recruitment", "RECRUIT"]
# “已经在公招页”的判据（列表页 / 词条面板都有）
RECRUIT_PAGE_KW = ["职业需求", "招募时限", "开始招募干员", "联络次数", "招募说明"]


class UIFlowError(RuntimeError):
    pass


class UI:
    def __init__(self, adb, ocr, cfg, data: RecruitData):
        self.adb = adb
        self.ocr = ocr
        self.cfg = cfg
        self.data = data
        self.delay = cfg["ui"].get("action_delay", 1.2)
        self.fast = cfg["ui"].get("fast_delay", 0.25)
        # 状态轮询间隔：每次「看一眼 → 决定下一步」之间歇多久。
        # 0.2s 已经比人眼快，再小就是空耗 CPU/抢焦点，不建议动。
        self.poll = float(cfg["ui"].get("poll_delay", 0.2))
        self.retry = cfg["ui"].get("retry", 3)
        self.width, self.height = self._resolution()
        self._last_img = None
        self.fastocr = FastOcr()
        self._tag_hits = {}          # 词条 → (x, y) 像素点，供 select_tags_fast 直接点
        self.claim_diag = ""         # 极速领取的诊断结果（dialog=槽位其实是空的）
        self._time_checked = False   # 本轮是否已经校验过时长（校验一次就够，省 0.66s/格）
        self._close_taps = 0         # 本次领取已经点过几次「关卡」
        self.locked_slots = set()    # 点了没反应 = 没解锁（人力办公室等级不够），之后不再试
        self.card_map = {}           # 实际卡片位置 {slot: {"center":(x,y), "button":(x,y)}}
        self._fast_time = None
        # 前台包名守卫：游戏被切后台/崩掉后会停在模拟器桌面，这时候再按坐标点
        # 就是在桌面上乱点（会打开别的 App / 把游戏彻底退出）。2026-11-03 实测。
        try:
            self.game_pkg = (cfg.get("game") or {}).get(
                "package", "com.hypergryph.arknights")
        except Exception:
            self.game_pkg = "com.hypergryph.arknights"
        self.dry_run = str(getattr(adb, "serial", "")).startswith("sim")
        self._last_fg = ""

    # ---------- 基础 ----------
    def _resolution(self) -> tuple:
        """返回 **input tap 坐标系** 的宽高。

        注意：`wm size` 报的是设备基准（竖屏）尺寸，横屏游戏时它和截图是
        转置关系（实测 900x1600 vs 截图 1600x900）。`input tap` 用的是当前
        旋转后的显示坐标，因此**必须**以截图尺寸为准，wm size 只作兜底。
        """
        sc = self.cfg["screen"]
        if sc.get("auto_resolution", True):
            wm = None
            try:
                wm = self.adb.wm_size()
            except Exception as e:
                log.warning("wm size 读取失败(%s)", e)
            try:
                h, w = self.adb.screencap().shape[:2]
                if wm and tuple(wm) != (w, h):
                    log.info("wm size=%sx%s，截图 %dx%d（横屏/旋转）→ 按截图坐标走",
                             wm[0], wm[1], w, h)
                log.info("分辨率: %dx%d（截图）", w, h)
                return w, h
            except Exception as e:
                log.warning("截图读分辨率失败(%s)", e)
            if wm:
                log.info("分辨率: %dx%d（wm size 兜底）", wm[0], wm[1])
                return wm
        return sc.get("default_width", 1280), sc.get("default_height", 720)

    def shot(self):
        self._last_img = self.adb.screencap()
        return self._last_img

    def save_shot(self, tag: str = "") -> str:
        ts = datetime.now().strftime("%H%M%S")
        os.makedirs(SHOT_DIR, exist_ok=True)
        name = os.path.join(SHOT_DIR, f"shot_{ts}_{tag}.png")
        try:
            import cv2
            img = self._last_img if self._last_img is not None else self.shot()
            if not hasattr(img, "shape"):      # dry-run 的 SimScreen：不是像素，跳过
                return ""
            ok, buf = cv2.imencode(".png", img)
            if not ok:
                raise RuntimeError("imencode 失败")
            with open(name, "wb") as f:      # 用 open 写：cv2.imwrite 遇到中文路径会静默失败
                f.write(buf.tobytes())
            log.info("截图已存: %s", name)
        except Exception as e:
            log.warning("截图保存失败: %s", e)
        return name

    def _tap(self, x, y, delay: float = None):
        self.adb.tap(x, y)
        time.sleep(self.delay if delay is None else delay)

    def _tap_pt(self, pt, delay: float = None):
        self._tap(pt[0], pt[1], delay)

    def _tap_text(self, keywords, tries: int = None, fail_ok: bool = False,
                  delay: float = None):
        """依次尝试多个关键词，找到就点其中心。返回命中的文本或 None。"""
        tries = self.retry if tries is None else tries
        for i in range(max(1, tries)):
            img = self.shot()
            for kw in keywords:
                t = self.ocr.find_text(img, kw)
                if t:
                    cx, cy = t.center
                    log.info("点击「%s」@(%.0f,%.0f)", t.text, cx, cy)
                    self._tap(cx, cy, delay)
                    return t.text
            if i < tries - 1:
                time.sleep(0.6)
        if fail_ok:
            return None
        raise UIFlowError(f"未找到按钮: {'/'.join(keywords)}")

    def wait(self, seconds: float):
        log.debug("等待 %.1fs", seconds)
        time.sleep(seconds)

    # ---------- 启动 / 导航 ----------
    def dismiss_popups(self, rounds: int = 2) -> int:
        """关掉登录奖励/公告/更新这类弹窗。返回关掉的个数。"""
        closed = 0
        for _ in range(rounds):
            img = self.shot()
            hit = None
            for kw in ["点击屏幕继续", "点击屏幕", "已知晓", "跳过", "关闭"]:
                t = self.ocr.find_text(img, kw)
                if t:
                    hit = t
                    break
            if not hit:
                break
            log.info("关闭弹窗: %s", hit.text)
            self._tap_pt(hit.center, delay=self.fast)
            closed += 1
            time.sleep(0.4)
        return closed

    def launch_game(self):
        """确保游戏在前台且真的可用（含加载页等待，不再无脑睡 60s）。"""
        g = self.cfg["game"]
        pkg = g.get("package", "com.hypergryph.arknights")
        try:
            cur = self.adb.current_package()
        except Exception:
            cur = ""
        if cur == pkg:
            log.info("游戏已在前台")
            self.wait_ready(timeout=8)
            return
        log.info("启动游戏: %s（当前前台: %s）", pkg, cur or "未知")
        self.adb.start_app(pkg, g.get("activity", ""))
        self.wait_ready(timeout=g.get("startup_timeout", 60))
        self.dismiss_popups(rounds=1)

    def _on_recruit_page(self) -> bool:
        """当前是否已经在公开招募页面（列表页或词条面板）。"""
        texts = self.ocr.texts(self.shot())
        return any(any(kw in t for kw in RECRUIT_PAGE_KW) for t in texts)

    def _on_wrong_panel(self) -> bool:
        """点入口时点歪了会进这些页（PC 端坐标差 15px 就点到「寻访历史记录」）。

        真机上踩过：点歪 → 卡在历史记录页 → 后续全部 OCR 失败 → 报「没找到公开招募」。
        这里先认出来、先关掉，再重点一次，比一口气重试到底靠谱。
        """
        try:
            texts = self.ocr.texts(self.shot())
        except Exception:
            return False
        wrong = ["历史记录", "寻访记录", "寻访历史", "干员寻访", "HEADHUNT", "寻访详情"]
        return any(any(k in t for k in wrong) for t in texts)

    def ensure_recruit_screen(self):
        """从任意界面回到「公开招募」界面。

        顺序：已在公招页 → 点入口文字（中/英）→ 点 config.ui.entry_recruit 坐标 → 返回到主页再试。
        """
        if self._on_recruit_page():
            log.info("已在公开招募界面")
            return

        if self._on_wrong_panel():
            log.info("当前在寻访/历史记录页 → 先关掉")
            self.cancel_panel()
            time.sleep(self.delay)

        if self._tap_text(RECRUIT_ENTRY_KW, tries=1, fail_ok=True, delay=self.delay):
            log.info("点了公招入口（文字）")
        else:
            node = self.cfg["ui"].get("entry_recruit")
            if node:
                x, y = node["x"] * self.width, node["y"] * self.height
                log.info("未识别到入口文字，按配置坐标点 @(%.0f,%.0f)", x, y)
                self._tap(x, y, delay=self.delay)

        for attempt in range(self.retry):
            if self._on_recruit_page():
                break
            if self._on_wrong_panel():
                log.info("点歪到寻访/历史记录页（第 %d 次）→ 关掉重试", attempt + 1)
                self.cancel_panel()
                time.sleep(self.delay)
            log.info("还没进公招页（第 %d 次），点左上返回箭头重试", attempt + 1)
            self.cancel_panel()
            time.sleep(self.delay)
            if not self._tap_text(RECRUIT_ENTRY_KW, tries=1, fail_ok=True, delay=self.delay):
                node = self.cfg["ui"].get("entry_recruit")
                if node:
                    self._tap(node["x"] * self.width, node["y"] * self.height, delay=self.delay)

        if not self._on_recruit_page():
            self.save_shot("no_recruit_entry")
            raise UIFlowError(
                "没找到「公开招募」入口。确认游戏停在主界面，"
                "或用 calibrate.py 校准入口坐标后填 config.ui.entry_recruit"
            )
        self.dismiss_popups(rounds=1)

    # ---------- 公招位 ----------
    def open_slot(self, i: int):
        """点开第 i 个公招位（1-based）。坐标来自 config.ui.slots（比例）。"""
        slots = self.cfg["ui"].get("slots") or []
        if i <= len(slots):
            s = slots[i - 1]
            x, y = s["x"] * self.width, s["y"] * self.height
            log.info("点开第 %d 个公招位 @(%.0f,%.0f)", i, x, y)
            self._tap(x, y)
        else:
            raise UIFlowError(f"config.ui.slots 只配置了 {len(slots)} 个坐标，无法打开第 {i} 位")

    def slot_state(self) -> str:
        """粗判当前公招位状态：可招募 / 进行中 / 未知。

        判据：出现词条面板文字（≥2 个词条）→ ready；
              出现「剩余」「加急许可」→ running。
        """
        img = self.shot()
        texts = self.ocr.texts(img)
        if any("加急" in t for t in texts) or any("剩余" in t for t in texts) or any("联络中" in t for t in texts):
            return "running"
        tags = extract_tags(texts, tags=list(self.data.tags.keys()) or None,
                           threshold=self.cfg["ocr"]["match_threshold"])
        return "ready" if len(tags) >= 2 else "unknown"

    def read_tags(self) -> list:
        """OCR 当前词条面板，返回识别出的公招词条。"""
        best = []
        for _ in range(self.retry):
            img = self.shot()
            texts = self.ocr.texts(img)
            tags = extract_tags(texts, tags=list(self.data.tags.keys()) or None,
                                threshold=self.cfg["ocr"]["match_threshold"])
            if len(tags) > len(best):
                best = tags
            if len(tags) >= 4:
                break
            time.sleep(0.6)
        log.info("识别词条(%d): %s", len(best), "、".join(best) if best else "无")
        return best

    def select_tags(self, combo: list):
        """点击选中 combo 里的词条。空 combo 合法（一个都不选，收益最优）。"""
        if not combo:
            log.info("按决策不选任何词条")
            return
        for tag in combo:
            hit = self._tap_text([tag], tries=2, fail_ok=True, delay=self.fast)
            if not hit:
                log.warning("词条「%s」精确匹配失败，用相似度兜底", tag)
                img = self.shot()
                cand = [(similarity(t.text, tag), t) for t in self.ocr.recognize(img)]
                cand.sort(key=lambda c: -c[0])
                if cand and cand[0][0] >= 0.6:
                    log.info("模糊匹配「%s」≈「%s」(%.2f)", tag, cand[0][1].text, cand[0][0])
                    self._tap_pt(cand[0][1].center, delay=self.fast)
                else:
                    raise UIFlowError(f"词条「{tag}」在界面上点不到（OCR 未命中）")
            time.sleep(0.25)

    # ---------- 时间 ----------
    _TIME_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?")
    BACK_ARROW = (0.037, 0.050)      # 公招页左上角返回箭头（千万别用 adb back，会退游戏）

    def _read_time_once(self):
        """读取「招募时限」里的时长，返回分钟数；读不到返回 None。

        ⚠️ 不能全屏抓第一个 HH:MM —— 页面右上角还有个「00:25:57 联络中」的
        倒计时，会被误读成 25 分钟（实测踩过）。所以先定位「招募时限」标签，
        只在它右同一行取时间。
        """
        items = self.ocr.recognize(self.shot())
        anchor = next((t for t in items if "招募时限" in t.text), None)
        if anchor is None:
            log.warning("没定位到「招募时限」标签 → 当前不在词条面板，拒绝读时长")
            return None
        ays = [p[1] for p in anchor.box] if anchor and anchor.box else None
        acy = sum(ays) / len(ays) if ays else None
        axr = max(p[0] for p in anchor.box) if anchor and anchor.box else None

        picked = []
        for t in items:
            if "联络中" in t.text:
                continue
            m = self._TIME_RE.search(t.text)
            if not m:
                m2 = re.search(r"(\d{1,2})\s*小时\s*(\d{1,2})?\s*分?", t.text)
                if not m2:
                    continue
                h, mi = int(m2.group(1)), int(m2.group(2) or 0)
            else:
                h, mi = int(m.group(1)), int(m.group(2))
            if not (0 <= h <= 24 and 0 <= mi < 60):
                continue
            cx, cy = t.center
            if acy is not None:
                if axr is not None and cx <= axr:      # 必须在「招募时限」右方
                    continue
                if abs(cy - acy) > 90:                  # 且同一行（±90px @1600x900）
                    continue
            picked.append((abs(cy - acy) if acy is not None else 0, h * 60 + mi, t.text))
        if not picked:
            return None
        picked.sort(key=lambda x: x[0])
        best = picked[0]
        log.info("时长读数: %s → %d 分钟", best[2], best[1])
        return best[1]

    def panel_state(self) -> str:
        """一次 OCR 判定槽位面板种类：

        placing —— 可挂单的词条面板（有「招募时限」时间控件）
        running —— 进行中的面板（只有「剩余 08:59:59」+「立即招募」）
        other   —— 没打开 / 还在列表页

        2026-11-03 踩坑：进行中的槽位点开后**也会显示当初选的词条**，
        旧逻辑会把它当空位去挂单，然后找「招募时限」找不到 → 整轮白跑。
        """
        texts = self.ocr.texts(self.shot())
        if any("招募时限" in t for t in texts):
            return "placing"
        if any(("立即招募" in t) or ("剩余" in t) for t in texts):
            return "running"
        return "other"

    def on_tag_panel(self) -> bool:
        """当前是否停在「词条/时长」面板（真机关键词：职业需求 + 招募时限）。

        2026-11-03 踩坑：开槽失败时会在**列表页**读时长，列表上「剩余 08:57:33」
        被当成招募时长 → set_time 报「时长停在 8:57」把整轮打断。
        所以读时长前必须先确认真的在面板上。
        """
        texts = self.ocr.texts(self.shot())
        return any("招募时限" in t for t in texts) or any("职业需求" in t for t in texts)

    def read_time_minutes(self, require_panel: bool = False):
        """稳健版：连读两遍，一致就用；不一致取大值（OCR 常把 09 读成 00/0）。

        实测踩坑：09:00:00 偶尔被读成「00:00:60」→ 0 分钟，若直接信它就误判失败。
        """
        vals = []
        for _ in range(2):
            v = self._read_time_once()
            if v is not None:
                vals.append(v)
            if len(vals) == 2 and vals[0] == vals[1]:
                break
        if not vals:
            return None
        if len(set(vals)) == 1:
            return vals[0]
        v = max(vals)
        log.warning("时长读数不稳 %s → 取大值 %d 分钟", vals, v)
        return v

    def cancel_panel(self):
        """关掉当前面板，回到列表页。

        ⚠️ 绝对不要 adb back()：在公招/主界面连按 back 会把游戏直接退掉
        （实测把游戏按崩重启，整轮作废）。所以只点面板内的 ✗ / 左上返回箭头。
        """
        if self.fast_ready():
            # 快速路径：面板就点 ✗，已经是列表就什么都不做（省掉一次 4~6s 全屏 OCR）
            kind = self.screen_kind()
            node = self.cfg["ui"].get("cancel_btn")
            if kind == "list":
                return
            if kind == "panel" and node:
                self._tap(node["x"] * self.width, node["y"] * self.height, delay=self.fast)
                self._last_img = None
                return
        texts = self.ocr.texts(self.shot())
        tag_panel = any(("招募时限" in t) or ("职业需求" in t) for t in texts)
        if tag_panel:
            node = self.cfg["ui"].get("cancel_btn")
            if node:
                self._tap(node["x"] * self.width, node["y"] * self.height, delay=self.fast)
                return
        if any(any(kw in t for kw in RECRUIT_PAGE_KW) for t in texts):
            # 已经在公招列表页：这里按返回键会把游戏退回大厅（2026-11-03 踩过），别动
            log.debug("已在公招列表页，无需关闭面板")
            return
        self._tap(self.BACK_ARROW[0] * self.width, self.BACK_ARROW[1] * self.height,
                  delay=self.delay)

    def _time_buttons(self):
        """返回 (+按钮坐标, -按钮坐标)，来自 config.ui.time_plus / time_minus。"""
        u = self.cfg["ui"]
        def pt(node):
            if not node:
                return None
            return (node["x"] * self.width, node["y"] * self.height)
        return pt(u.get("time_plus")), pt(u.get("time_minus"))

    def set_time(self, hours: int = 9):
        """把招募时长设到 hours 小时。

        策略：
          1. 若界面直接有「9小时」这类快捷项 → 点它；
          2. 否则读当前时间，点「+」逐档加到目标（每档 config.ui.time_step_min 分钟）；
          3. 到顶仍不到目标 → 用「−」回退到目标（防越界）。
        """
        target = int(hours) * 60
        quick = [f"{hours}小时", f"{hours}小时0分", f"{hours}:00", f"0{hours}:00"]
        if self._tap_text(quick, tries=1, fail_ok=True, delay=self.fast):
            log.info("时间已设为 %d 小时（快捷项）", hours)
            return

        plus, minus = self._time_buttons()
        step = self.cfg["ui"].get("time_step_min", 10)
        max_taps = self.cfg["ui"].get("time_taps_max", 70)

        cur = self.read_time_minutes()
        if cur is None:
            if plus is None:
                self.save_shot("no_time_widget")
                raise UIFlowError(
                    "读不到招募时长，也没配置 ui.time_plus 坐标。"
                    "请跑 calibrate.py 采集时间「+」按钮位置"
                )
            cur = 60
            log.warning("读不到时长，假定当前 01:00")

        if cur >= target:
            log.info("当前时长 %.2fh 已达标", cur / 60)
            return

        need = max(0, (target - cur) // step)
        log.info("时长 %d 分钟 → 目标 %d 分钟，需点 + %d 次", cur, target, need)
        if plus is None:
            self.save_shot("no_time_plus")
            raise UIFlowError("缺少 ui.time_plus 坐标，无法调整时长")

        taps = min(need, max_taps)
        last = cur
        for i in range(taps):
            self.adb.tap(*plus)
            time.sleep(self.cfg["ui"].get("tap_time_delay", 0.12))
            if i % 8 == 7 or i == taps - 1:          # 每 8 下校验一次（每次校验≈5s OCR）
                now = self.read_time_minutes()
                if now is None:
                    continue
                if now <= last:
                    log.warning("点 + 后时长没变（%d 分钟）→ 可能已到上限，停手", now)
                    break
                last = now
                if now >= target:
                    break

        now = self.read_time_minutes()
        for _ in range(10):                          # 双向微调，带次数上限，禁止死循环
            if now is None or now == target:
                break
            if now > target and minus:
                self.adb.tap(*minus)
            elif now < target and plus and now > 0:
                self.adb.tap(*plus)
            else:
                break
            time.sleep(self.cfg["ui"].get("tap_time_delay", 0.12))
            now = self.read_time_minutes()

        if now is None:
            self.save_shot("time_unreadable")
            raise UIFlowError("读不出招募时长 —— 当前界面不是词条面板？先别点确认")
        if now != target:
            self.save_shot(f"time_bad_{now}")
            raise UIFlowError(
                f"时长停在 {now // 60}:{now % 60:02d}，不是目标 {target // 60}:00 —— "
                f"不点确认，先校 config.ui.time_plus / time_step_min"
            )
        log.info("时长已设为 %d:%02d", now // 60, now % 60)

    # ---------- 确认 / 加急 / 结果 ----------
    def confirm(self):
        """点「开始招募」并吃掉紧接着的二次确认弹窗。

        游戏里点开始招募会弹「确认/取消」框，只点第一下等于没招募。
        新版词条面板没有文字按钮，只有一个蓝色 ✓（config.ui.confirm_btn）。
        """
        hit = self._tap_text(["开始招募", "确认招募", "招募开始"], tries=2, fail_ok=True)
        if not hit:
            node = self.cfg["ui"].get("confirm_btn")
            if node:
                x, y = node["x"] * self.width, node["y"] * self.height
                log.info("词条面板无文字按钮 → 点 ✓ @(%.0f,%.0f)", x, y)
                self._tap(x, y, delay=self.delay)
                hit = True
        if not hit:
            # 有些界面直接是「确认」按钮（无二次确认）
            self._tap_text(["确认", "确定"])
            self.wait(self.delay)
            return
        # 二次确认：弹窗消失前最多试 retry 次；找不到说明游戏没弹窗
        for _ in range(2):
            time.sleep(0.5)
            img = self.shot()
            if self.ocr.find_text(img, "开始招募") and not self.ocr.find_text(img, "取消"):
                break
            if not self._tap_text(["确认", "确定"], tries=1, fail_ok=True, delay=self.delay):
                break
        time.sleep(self.fast)

    def use_expedite(self, slot: int = None) -> bool:
        """用加急许可立即完成招募。返回是否点到了。

        2026-11 实测：进行中的槽位按钮叫「立即招募」（不是「加急许可」），
        点完弹「是否消耗一张加急许可立即完成招募?」—— 弹窗没有文字按钮，
        只有一个白色 ✓（config.ui.expedite_confirm）。
        """
        kws = ["立即招募", "加急许可", "加急", "立即完成"]
        hit = None
        for attempt in range(2):
            hit = self._tap_text(kws, tries=1, fail_ok=True, delay=self.fast)
            if hit:
                break
            if slot:
                self.open_slot(slot)
                hit = self._tap_text(kws, tries=1, fail_ok=True, delay=self.fast)
                if hit:
                    break
            time.sleep(self.fast)
        if not hit:
            # 没按钮也可能是「已经加急过了、正在等领人」——别急着判定耗尽
            texts = self.ocr.texts(self.shot())
            if any("聘用候选人" in t or "已找到" in t for t in texts):
                log.info("槽位已是「待领取」状态，加急其实已完成")
                return True
            if slot:
                self.cancel_panel()
            log.warning("没找到「立即招募」入口（加急许可可能真的用完了）")
            return False

        time.sleep(self.fast)
        # 同一道安全闸（慢速路径也要）：右侧确认点会落在「停止招募」弹窗的
        # 「确认停止」红按钮上 —— 先认出来，只点左侧取消。
        try:
            dial = self._dialog_text(self.shot(), deep=True)
        except Exception:
            dial = ""
        if "停止" in dial or "放弃" in dial:
            log.warning("这是「停止招募」确认框 → 点取消，绝不停掉挂好的招募")
            self.adb.tap(0.23 * self.width, 0.646 * self.height)
            time.sleep(0.25)
            self._last_img = None
            return False
        if not self._tap_text(["确认", "确定", "使用"], tries=1, fail_ok=True, delay=self.delay):
            node = self.cfg["ui"].get("expedite_confirm")
            if node:
                x, y = node["x"] * self.width, node["y"] * self.height
                log.info("加急确认弹窗无文字按钮 → 点 ✓ @(%.0f,%.0f)", x, y)
                self._tap(x, y, delay=self.delay)
            else:
                log.warning("加急弹窗既无文字按钮也没配 ui.expedite_confirm")
                return False
        return True

    def claim_candidates(self) -> bool:
        """加急完成后槽位显示「已找到符合部分要求的候选人」→ 点「聘用候选人」看结果。

        这是流程里最容易漏的一步：不点它，出货永远读不到。
        """
        self._close_taps = 0             # 新的领取 → 关卡次数重新计数
        for _ in range(self.retry):
            if self._tap_text(["聘用候选人"], tries=1, fail_ok=True,
                              delay=self.fast):
                log.info("点「聘用候选人」→ 进结果页（黑箱动画开始，SKIP 交给 read_result）")
                self.wait_result(timeout=6.0)   # 尽量等动画过一帧再返回
                return True
            time.sleep(0.6)
        return False

    # ---------- 出货结果 ----------
    def _ocr_name_region(self, img) -> list:
        """裁「干员名牌」区域放大 2x 再 OCR（名牌是艺术字，整屏 OCR 常漏）。"""
        node = self.cfg["ui"].get("result_name") or             {"x1": 0.30, "y1": 0.70, "x2": 0.78, "y2": 0.93}
        try:
            import cv2
            h, w = img.shape[:2]
            x1, y1 = int(node["x1"] * w), int(node["y1"] * h)
            x2, y2 = int(node["x2"] * w), int(node["y2"] * h)
            crop = img[y1:y2, x1:x2]
            if crop.size == 0:
                return []
            big = cv2.resize(crop, None, fx=2.0, fy=2.0,
                             interpolation=cv2.INTER_CUBIC)
            return self.ocr.texts(big)
        except Exception as e:
            log.debug("名牌区域 OCR 失败: %s", e)
            return []

    def _match_operator(self, texts) -> tuple:
        """在一屏 OCR 文本里找最像的干员名 → (name, rarity, score)。"""
        thr = self.cfg["ocr"].get("match_threshold", 0.7)
        best, best_score, best_rarity = None, 0.0, 0
        for raw in texts or []:
            t = (raw or "").strip()
            if not t or not any("一" <= ch <= "鿿" for ch in t):
                continue          # 干员名都是中文：先把 SKIP/HUMAN/12345 这类噪声剔掉
            for op in self.data.operators:
                sc = similarity(t, op["name"])
                if sc > best_score:
                    best, best_score, best_rarity = op["name"], sc, op["rarity"]
        return best, best_rarity, best_score

    def _result_scan(self) -> tuple:
        """截图 → 整屏 OCR → 名牌区域兜底 → (texts, name, rarity)。"""
        img = self.shot()
        texts = self.ocr.texts(img)
        name, rarity, score = self._match_operator(texts)
        thr = self.cfg["ocr"].get("match_threshold", 0.7)
        if name and score >= thr:
            return texts, name, rarity
        crop_texts = self._ocr_name_region(img)
        name2, rarity2, score2 = self._match_operator(crop_texts)
        if name2 and score2 >= thr:
            return texts + crop_texts, name2, rarity2
        return texts, None, 0

    def _tap_skip(self) -> bool:
        """点结果动画右上角的 SKIP（OCR 定位优先，坐标兜底）。"""
        img = self._last_img if self._last_img is not None else self.shot()
        t = self.ocr.find_any(img, ["SKIP", "跳过"])
        if t:
            log.info("点 SKIP @(%.0f,%.0f)", *t.center)
            self._tap_pt(t.center, delay=self.fast)
            return True
        node = self.cfg["ui"].get("skip_btn")
        if node:
            log.info("点 SKIP（坐标兜底）")
            self._tap(node["x"] * self.width, node["y"] * self.height,
                      delay=self.fast)
            return True
        return False

    # ---------- 结果页 / 开包页 推进（快进） ----------
    def _skip_band_text(self, img=None) -> str:
        """读右上角那条 SKIP 窄带（单行 rec，≈60ms）。"""
        return self._rec({"x1": 0.86, "y1": 0.02, "x2": 0.999, "y2": 0.13}, img)

    # 右侧奖励栏三条单行读带（2026-11-08 实测）：一整块 0.42~0.72 的读带交给
    # FastOcr（只 rec 不检测）只能读出「上」一个字 → 出货卡判不出来 → 状态机
    # 点中央把卡点穿 → 卡片被清空、出货没记账（就是用户看到的「第 4 格轮空」）。
    REWARD_BANDS = (
        {"x1": 0.88, "y1": 0.44, "x2": 0.99, "y2": 0.50},   # 重复获得 +30
        {"x1": 0.88, "y1": 0.52, "x2": 0.99, "y2": 0.58},   # 资质凭证
        {"x1": 0.88, "y1": 0.60, "x2": 0.99, "y2": 0.66},   # 重复获得 +1
        {"x1": 0.88, "y1": 0.68, "x2": 0.99, "y2": 0.74},   # 刻刀的信物
    )

    def _reward_text(self, img=None) -> str:
        """读右侧奖励栏（出货卡的唯一稳定信号）。逐条单行 rec，必要时带检测兜底。"""
        img = img if img is not None else self._frame()
        parts = []
        for band in self.REWARD_BANDS:
            t = self._rec(band, img)
            if t:
                parts.append(t)
        txt = " ".join(parts)
        if not any(k in txt for k in ("重复", "凭证", "信物", "获得")):
            try:
                crop = self._crop({"x1": 0.85, "y1": 0.40, "x2": 1.0, "y2": 0.75}, img)
                txt += " " + " ".join(self.ocr.texts(crop))
            except Exception as e:
                log.debug("奖励栏 det-OCR 失败: %s", e)
        return txt

    def _result_frame_like(self, img=None) -> bool:
        """这一帧「像出货卡」吗——**不看名字**，只看卡片固定元素。

        为什么需要它：出货卡点一下就关。如果名字 OCR 读失败（艺术字/字号/遮挡），
        脚本会以为「还没到卡」→ 点中央 → 把卡点掉 → 这一格出货就丢了。
        所以推进之前先用两个更稳的信号兜底：
          1) 右侧奖励条（重复获得 / 资质凭证 / 信物）
          2) 职业英文行（SNIPER / VANGUARD …，ASCII 很稳）
        """
        img = img if img is not None else self._frame()
        rew = self._reward_text(img)
        if any(k in rew for k in ("重复获得", "重复", "资质凭证", "信物")):
            return True
        cls = self._rec({"x1": 0.345, "y1": 0.685, "x2": 0.465, "y2": 0.735}, img)
        return bool(self._ascii_word(cls) and len(cls.strip()) >= 3)

    def advance_result(self, deep: bool = False) -> str:
        """把「黑箱动画 / 开干员包」这一屏推进一步（快进）。

        2026-11-07 实机症状：加急券用掉后不是直接到出货卡，中间有一屏
        「开干员包」，**整屏没有 SKIP 文字**，状态机认不出来 → 干等 9s → 判失败
        → 这一格的出货白丢。这屏点屏幕任意处就能快进，所以按三级推进：

          1) 右上 SKIP（窄带 rec，最快）
          2) 屏幕上的「点击继续 / 继续 / 快进」文字（deep 时才整屏 OCR，慢）
          3) 兜底：点屏幕中央（开箱 / 过场通用，实测最有效）

        安全闸：**先确认不在** 列表 / 词条面板 / 大厅 / 基建，否则一下都不点
        （否则点中央正好落到某个公招格上，把后面的流程全打乱）。
        返回实际做了什么：skip / text / center / none
        """
        if self.dry_run:                    # 离线模拟没有像素，别乱点
            return "none"
        kind = self.screen_kind()
        if kind in ("list", "panel", "lobby", "base", "headhunt"):
            log.debug("advance_result: 当前是 %s → 不推", kind)
            return "none"

        u = self.cfg["ui"]
        img = self._frame()

        if self._result_frame_like(img):
            # 卡片已经在屏幕上，只是名字没认出来 → 千万别点（点一下就关，出货就丢了）
            return "none"

        band = self._skip_band_text(img).upper()
        if "SKIP" in band or "跳过" in band:
            sk = u.get("skip_btn") or {"x": 0.956, "y": 0.073}
            self.adb.tap(sk["x"] * self.width, sk["y"] * self.height)
            return "skip"

        if deep:
            kws = u.get("result_kw") or ["点击继续", "点击屏幕", "继续", "快进", "跳过"]
            t = self.ocr.find_any(img, kws)
            if t:
                log.info("点「%s」快进 @(%.0f,%.0f)", t.text, *t.center)
                self._tap_pt(t.center, delay=self.fast)
                return "text"

        node = u.get("result_nudge") or {"x": 0.5, "y": 0.5}
        self.adb.tap(node["x"] * self.width, node["y"] * self.height)
        return "center"

    def wait_result(self, timeout: float = 10.0, nudge: bool = True) -> bool:
        """结果页是否已出现干员名；顺便把卡住的开包页推快进。"""
        end = time.time() + timeout
        nudges = 0
        while time.time() < end:
            _, name, _ = self._result_scan()
            if name:
                return True
            if nudge and nudges < 6:
                self.advance_result(deep=(nudges == 2))
                nudges += 1
            time.sleep(0.35)
        return False

    def read_result(self, timeout: float = 22.0) -> tuple:
        """取本次出货 → (name, rarity)。

        2026-11-03 实测的坑：点完「聘用候选人」先播黑箱装证件动画，
        整屏 OCR 只有 ['SKIP','HUMAN','RESOURCE','DEPARTMENT']，此时读人必然失败。

        2026-11-07 又踩一个新坑：加急券用掉后会多出一屏「开干员包」，
        这一屏没有 SKIP 文字，光「边点 SKIP 边轮询」会干等到超时 → 判定识别失败。
        现在每一步都交给 advance_result()：SKIP → 文字按钮 → 点中央，三级推进。
        """
        end = time.time() + timeout
        nudges, skips, last_texts = 0, 0, []
        while time.time() < end:
            texts, name, rarity = self._result_scan()
            last_texts = texts
            if name:
                log.info("结果页识别到干员: %d★ %s", rarity, name)
                return name, rarity
            # SKIP 优先（动画还在播）——这条路径离线模拟也走，测试靠它推进
            if skips < 8 and any("SKIP" in t.upper() for t in texts):
                self._tap_skip()
                skips += 1
                continue
            if nudges < 12:
                how = self.advance_result(deep=(nudges % 3 == 2))
                if how != "none":
                    nudges += 1
            time.sleep(0.35)
        log.warning("未识别出货干员（%.0fs 超时，推进 %d 次），原始 OCR: %s",
                    timeout, nudges, last_texts)
        return None, 0

    def close_result(self) -> bool:
        """关掉出货卡，回到公招位列表。

        结果卡是全屏立绘、没有按钮：点左下空白再点屏幕中央即可退出。
        退出成功判据 = 公招列表页关键字（RECRUIT_PAGE_KW）重新出现。

        2026-11-07：出货卡后面还可能跟一屏「干员介绍」，点一下就快进 ——
        先让 skip_result_pages() 快速点穿，剩下的再走这里的老逻辑兜底。
        """
        try:
            if self.skip_result_pages(max_taps=4, timeout=3.5):
                return True
        except Exception as e:
            log.debug("快进过渡页失败（继续走老逻辑）: %s", e)
        node = self.cfg["ui"].get("result_close") or {"x": 0.125, "y": 0.889}
        for _ in range(self.retry + 1):
            if any(kw in t for t in self.ocr.texts(self.shot()) for kw in RECRUIT_PAGE_KW):
                return True
            self._tap_text(["确认", "确定", "知道了", "点击继续", "继续"],
                           tries=1, fail_ok=True, delay=self.fast)
            if not self.tap_result_close():
                break                       # 关卡次数用完了 → 不再空点
            before = self._frame()
            self.adb.tap(0.5 * self.width, 0.5 * self.height)
            if not self._wait_change(before):
                break                      # 点了没反应 → 别再空点
        log.warning("出货卡可能没关掉（未见公招页关键字）")
        return False

    # ================= 极速路径（1600x900 标定，目标 <6s/槽） =================
    def fast_ready(self) -> bool:
        """极速裁块只在「标定分辨率」上有效，其它分辨率老实用慢速全屏 OCR。

        默认 1600x900；在别的分辨率上重新标定后，把 config.ui.fast.at 改成对应值即可
        （PC 端客户区常常不是 1600x900，这条能省掉「永远走慢速」的坑）。
        """
        f = (self.cfg["ui"] or {}).get("fast")
        if not f:
            return False
        at = f.get("at") or [1600, 900]
        try:
            if (self.width, self.height) == (int(at[0]), int(at[1])):
                return True
        except Exception:
            if self.width == 1600 and self.height == 900:
                return True
        # 可选：同 16:9 的其它分辨率（1920x1080 / 1280x720 / 2560x1440…）也允许走极速路径。
        # 坐标全是比例，16:9 下等比缩放后仍然对得上；但个别客户端会加黑边/改布局，
        # 所以默认关闭，要在 config.ui.fast.allow_any_16_9 里显式打开并自己验证一轮。
        if f.get("allow_any_16_9"):
            return abs(self.width / max(1, self.height) - 16 / 9) <= 0.02
        return False

    def _frame(self):
        """取当前帧：有缓存就用缓存（截图 0.66s，能省一次是一次）。"""
        return self._last_img if self._last_img is not None else self.shot()

    def _crop(self, node, img=None):
        img = img if img is not None else self._frame()
        h, w = img.shape[:2]
        x1 = int(node.get("x1", 0.0) * w)
        y1 = int(node.get("y1", 0.0) * h)
        x2 = int(node.get("x2", 1.0) * w)
        y2 = int(node.get("y2", 1.0) * h)
        return img[y1:y2, x1:x2]

    def _rec(self, node, img=None) -> str:
        """裁一小块做单行 rec（≈60ms）。读不到/裁剪失败一律当空串，不抛异常。"""
        try:
            return (self.fastocr.line(self._crop(node, img)) or "").strip()
        except Exception as e:
            log.debug("裁剪 OCR 失败: %s", e)
            return ""

    # ---------- 前台 / 页面识别 ----------
    DIALOG_KW = ("是否", "确定", "取消", "不足", "上限", "耗尽", "不够", "确认")

    def in_game(self) -> bool:
        """游戏是否在**当前前台**。不在就一个坐标都不许点（否则点的是模拟器桌面）。"""
        if self.dry_run:
            return True
        try:
            cur = (self.adb.current_package() or "").strip()
        except Exception as e:
            log.debug("读前台包名失败(%s) → 不判定", e)
            return True
        if not cur:
            return True                      # 读不到就别乱判定，交给截图重试兜底
        self._last_fg = cur
        return cur == self.game_pkg

    def _dialog_text(self, img=None, deep: bool = False) -> str:
        """读屏幕中部的弹窗文字（加急确认 / 奖励 / 联络次数 / 退出确认都在这一带）。

        2026-11-03 踩坑：弹窗是**整条横幅**，拿一大块去跑无检测的单行 rec 会读成
        '。'（实测 logs/after_expedite.png），于是「弹窗明明在，dial=''」。
        这里改成先试三条单行窄带，都不行再上「带检测」的 OCR（deep=True）。
        """
        img = img if img is not None else self._frame()
        # 2026-11-08 实测（停止招募确认框）：长文案弹窗的问句行在 y≈0.44，
        # 旧的三条读带（0.525~0.645）**一条都读不到**（实测全空串）→ 屏幕被当成
        # other → 没人知道这是「停止招募」确认框，后续确认点又正好落在
        # 「确认停止」红按钮上 → 已挂好的招募被直接停掉。
        for band in ({"x1": 0.20, "y1": 0.395, "x2": 0.80, "y2": 0.465},
                     {"x1": 0.30, "y1": 0.545, "x2": 0.70, "y2": 0.630},
                     {"x1": 0.20, "y1": 0.525, "x2": 0.80, "y2": 0.600},
                     {"x1": 0.20, "y1": 0.560, "x2": 0.80, "y2": 0.645}):
            t = self._rec(band, img)
            if t and len(t) >= 3:
                return t
        if deep:
            try:
                return " ".join(self.ocr.texts(
                    self._crop({"x1": 0.10, "y1": 0.38, "x2": 0.90, "y2": 0.80}, img)))
            except Exception as e:
                log.debug("弹窗 det-OCR 失败: %s", e)
        return ""

    def screen_kind(self) -> str:
        """一眼给当前界面分类（小裁切 rec，各 ≈60ms）。

        返回：list / panel / dialog / result / lobby / base / other

        2026-11-03 事故复盘：以前「必须认出公招列表页」才允许动作，OCR 一次没
        读准（小裁切对背景、动画、提示气泡都敏感）就判成「不在列表页」→ 自愈 →
        连点返回箭头 → 把游戏一路退回模拟器桌面，然后对着桌面继续点坐标，
        又打开了别的 App（用户看到的"一直弹窗/乱跳"）。
        所以这里**读不到就返回 other，不伪造"不是列表页"**，让上层做安全兜底。
        """
        u = self.cfg["ui"].get("fast") or {}
        img = self._frame()
        lab = self._rec(u.get("label") or
                        {"x1": 0.17, "y1": 0.28, "x2": 0.29, "y2": 0.34}, img)
        if "招募时限" in lab or "职业需求" in lab:
            return "panel"
        head = self._rec(u.get("list_head") or
                         {"x1": 0.02, "y1": 0.16, "x2": 0.26, "y2": 0.25}, img)
        # 容错：裁块偏一点时「公开招募」常被读成「公开招莫」/「公开招摹」。
        # 2026-11-08 实测四帧全部读成「公开招莫」→ 旧关键字匹配不上 → 判 other
        # → advance_result 在列表页上狂点中央（就是用户看到的「空点几下」）。
        if any(k in head for k in ("公开招募", "公开", "招募")):
            # 面板抬头也是「招募说明」——用中间那块大号时长区分
            if "说明" in head:
                tim = self._parse_minutes(self._rec(
                    u.get("time") or {"x1": 0.29, "y1": 0.27, "x2": 0.68, "y2": 0.36}, img))
                if tim is not None:
                    return "panel"
            return "list"
        # 寻访 / 寻访详情 / 干员详情页（2026-11-08 实机）：这两个页面以前全落进
        # other，上层「轻点中央推进」就在干员立绘上乱点，最后点到「停止招募」。
        # 必须排在 on_result_card 之前 —— 寻访页上有一堆干员名，会被误判成「出货卡」，
        # 然后 close_result 去点 (0.125,0.889)，在那页正好是「查看详情」。
        # 底栏「寻访一次 / 寻访十次」+ 横幅「概率提升 / 查看详情」两条读带就够了。
        hh_btn = self._rec({"x1": 0.58, "y1": 0.82, "x2": 0.90, "y2": 0.92}, img)
        hh_txt = self._rec({"x1": 0.08, "y1": 0.65, "x2": 0.52, "y2": 0.75}, img)
        if any(k in hh_btn for k in ("寻访", "寻访一次", "寻访十次")) or \
                any(k in hh_txt for k in ("概率提升", "寻访详情", "查看详情", "选调")):
            return "headhunt"
        # 抬头没读到时，可能是个挡住流程的确认弹窗（加急/联络/奖励）
        dial = self._dialog_text(img)
        if any(k in dial for k in self.DIALOG_KW):
            return "dialog"
        if self.on_result_card():
            return "result"
        if self.on_lobby():
            return "lobby"
        if self.on_base_screen():
            return "base"
        # 黑箱装证件动画：整屏只有右上角一个 SKIP（2026-11-04 实机卡死在这）
        sk = self._rec({"x1": 0.90, "y1": 0.03, "x2": 0.995, "y2": 0.105}, img)
        if "SKIP" in sk.upper():
            return "anim"
        # 抬头没读到时看卡片按钮（进行中卡片有「立即招募」，待领有「聘用候选人」）
        for card in (u.get("cards") or [])[:2]:
            e = card.get("expedite") or {}
            b = self._rec({"x1": e.get("x", 0) - 0.05, "x2": e.get("x", 0) + 0.05,
                           "y1": e.get("y", 0) - 0.025, "y2": e.get("y", 0) + 0.025}, img)
            if ("立即" in b) or ("招募" in b) or ("聘用" in b):
                return "list"
        return "other"

    def ensure_foreground(self) -> bool:
        """确保游戏在前台；掉到桌面/别的 App 就重新拉起并等它到可用界面。"""
        if self.in_game():
            return True
        log.warning("游戏不在前台（当前: %s）→ 重新拉起", self._last_fg or "未知")
        try:
            self.launch_game()
        except Exception as e:
            log.error("重新拉起游戏失败: %s", e)
            return False
        return self.in_game()

    def wait_ready(self, timeout: float = 60) -> bool:
        """等游戏真正可用（大厅/列表/面板）——**加载页上只轻点，不按返回**。

        2026-11-03 20:56~20:59 事故：游戏停在「炎国」加载页，脚本当大厅处理，
        点入口坐标（没反应）→ 点左上返回（把游戏退了）→ 又对桌面点坐标。
        """
        if self.dry_run:
            return True
        if not self.fast_ready():
            time.sleep(min(max(timeout, 5.0), 20.0))
            self.dismiss_popups(rounds=1)
            return True
        end = time.time() + max(5.0, float(timeout))
        while time.time() < end:
            if not self.in_game():
                time.sleep(1.0)
                continue
            self._last_img = None
            k = self.screen_kind()
            if k in ("lobby", "list", "panel", "result"):
                log.info("游戏就绪（%s）", k)
                return True
            if k == "dialog":
                self.dismiss_dialog()
                continue
            # 加载页 / 登录页 / 公告页：轻点中央推进（返回键会把游戏退掉）
            self.adb.tap(0.5 * self.width, 0.55 * self.height)
            time.sleep(1.5)
        log.warning("等了 %.0fs 还没到可用界面（当前 %s）", timeout, self.screen_kind())
        return False

    @staticmethod
    def _parse_minutes(text: str):
        """'09:00:00' / '9：00' / '01:00:00' → 分钟；解析不出返回 None。"""
        if not text:
            return None
        t = text.replace("：", ":").replace(" ", "")
        m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
        if not m:
            return None
        h = int(m.group(1))
        mi = int(m.group(2))
        if not (0 <= h <= 24 and 0 <= mi < 60):
            return None
        return h * 60 + mi

    def _quick_line(self, key, img=None) -> str:
        return self.fastocr.line(self._crop(self.cfg["ui"]["fast"][key], img)).strip()

    def enter_recruit_fast(self, tries: int = 3) -> bool:
        """从大厅一步进公招（坐标 + 60ms 校验），比全屏 OCR 找入口快一个数量级。"""
        if not self.in_game():
            log.warning("不在游戏前台 → 不点公招入口")
            return False
        node = self.cfg["ui"].get("entry_recruit") or {"x": 0.789, "y": 0.706}
        for i in range(max(1, tries)):
            self.adb.tap(node["x"] * self.width, node["y"] * self.height)
            time.sleep(0.9)
            self._last_img = None
            if self.screen_kind() == "list":
                return True
            if i == 0:
                # 大厅盖着活动/公告页 → 点屏幕中央（「点击继续」）再点一次入口
                self.adb.tap(0.5 * self.width, 0.60 * self.height)
                time.sleep(0.6)
                self._last_img = None
        self._last_img = None
        return False

    def ensure_list_fast(self, attempts: int = 3) -> bool:
        """确保停在公招列表页（快速版：小裁切分类 + 针对性动作）。"""
        if not self.fast_ready():
            return self._ensure_list_slow(attempts)
        u = self.cfg["ui"]
        cancel = u.get("cancel_btn") or {"x": 0.753, "y": 0.902}
        if not self.in_game():
            log.warning("游戏不在前台 → 交回上层重新拉起（不在桌面上乱点）")
            return False
        for _ in range(max(1, attempts)):
            self._last_img = None
            kind = self.screen_kind()
            if kind == "list":
                return True
            if kind == "panel":
                log.info("词条面板还开着 → 点 ✗ 回列表")
                self.adb.tap(cancel["x"] * self.width, cancel["y"] * self.height)
                time.sleep(0.35)
            elif kind == "dialog":
                self.dismiss_dialog()
            elif kind == "result":
                log.info("停在出货卡上 → 先点掉")
                self.close_result_fast()
            elif kind == "anim":
                log.info("黑箱动画卡着 → 点 SKIP")
                sk = u.get("skip_btn") or {"x": 0.956, "y": 0.073}
                self.adb.tap(sk["x"] * self.width, sk["y"] * self.height)
                time.sleep(0.6)
            elif kind == "base":
                self.leave_base()
            elif kind == "headhunt":
                # 寻访/寻访详情页：只按 ESC 退出来，绝不在立绘上点中央
                # （2026-11-08：以前这里落到 other → 点中央 → 一路点到「停止招募」）
                log.info("误入寻访页 → 按返回键退出")
                try:
                    self.adb.back()
                except Exception as e:
                    log.debug("按返回失败: %s", e)
                time.sleep(0.6)
            elif kind == "lobby":
                log.info("在大厅 → 直接进公开招募")
                if self.enter_recruit_fast(tries=1):
                    return True
            else:
                # 加载页/教程/未知：只轻点屏幕中央推进，绝不按返回（会退游戏）
                log.info("界面未识别（%s）→ 等待 / 只轻点中央", kind)
                before = self._frame()
                self.adb.tap(0.5 * self.width, 0.55 * self.height)
                if not self._wait_change(before, timeout=1.2):
                    # 点了界面纹丝不动 → 再点就是在盲戳，宁可停下等上层干预
                    log.warning("界面没反应（%s）→ 停止盲点", kind)
                    break
        self._last_img = None
        return self.screen_kind() == "list"

    def _ensure_list_slow(self, attempts: int = 3) -> bool:
        """慢速兜底（非 1600x900 / dry-run）：全屏 OCR 判定 + 安全点关闭。

        2026-11-03 踩坑：✓ 没点成功时面板会留在屏幕上，后面的点击全打在面板上，
        再叠上「干员档案」这类教程弹窗，就彻底迷路（用户看到的"一直弹弹窗"）。
        """
        u = self.cfg["ui"]
        cancel = u.get("cancel_btn") or {"x": 0.753, "y": 0.902}
        for _ in range(max(1, attempts)):
            head = self._quick_line("list_head")
            if ("公开招募" in head) or ("招募" in head):
                if self._parse_minutes(self._quick_line("time")) is None:
                    self._last_img = None
                    return True
            # 只点两个「安全位置」：招募面板的 ✗、左上返回箭头。
            # 绝不在未知界面乱点（2026-11-03 就是这样点进了商店/设置/干员档案，
            # 还弹出支付确认——这就是用户看到的"窗口一直弹"）。
            if self.on_base_screen():          # 误入基建：先退出来
                self.leave_base()
                continue
            if self.on_result_card():
                log.info("停在出货卡上 → 先点掉")
                self.close_result_fast()
                continue
            if self.on_lobby():
                # 大厅上点「返回箭头」会弹「确定退出游戏?」——绝对不能点，
                # 交给上层走 enter_recruit_fast
                log.info("当前在大厅 → 直接进公开招募")
                self._last_img = None
                return False
            log.info("界面不在公招列表 → 点 ✗ / 返回箭头")
            self.adb.tap(cancel["x"] * self.width, cancel["y"] * self.height)
            time.sleep(0.35)
            self._last_img = None
            head = self._quick_line("list_head")
            if ("公开招募" in head) or ("招募" in head):
                self._last_img = None
                return True
            self.adb.tap(0.05 * self.width, 0.05 * self.height)
            time.sleep(0.40)
            self._last_img = None
        head = self._quick_line("list_head")
        self._last_img = None
        return ("公开招募" in head) or ("招募" in head)

    @staticmethod
    def _ascii_word(t: str) -> bool:
        t = (t or "").strip()
        return len(t) >= 3 and all(ord(c) < 128 for c in t) and any(c.isalpha() for c in t)

    def on_result_card(self) -> bool:
        """是否停在出货卡上。

        需要**两条证据同时成立**，否则动画/转场帧会被误判（2026-11-04 实机：
        转场时职业行读成 'OALRR' 也算 ASCII → 误判成出货卡 → 名字读空、白丢一次出货）：
          1) 右侧奖励条（重复获得 / 资质凭证 / 信物）—— 重复出货必有；
          2) 职业英文行（SNIPER/VANGUARD…）+ 名字条能对上干员库 —— 首次出货也有。
        """
        img = self._frame()
        rew = self._reward_text(img)
        if any(k in rew for k in ("重复获得", "重复", "资质凭证", "信物")):
            return True
        cls = self._rec({"x1": 0.345, "y1": 0.685, "x2": 0.465, "y2": 0.735}, img)
        line = self._quick_line("name", img)
        # 证据二：职业英文行 + 名字条能对上
        if self._ascii_word(cls) and len(cls.strip()) >= 3:
            name, _r = self._match_name(line)
            if name:
                return True
        # 证据三（2026-11-08 加）：新干员的「加入」界面没有职业英文行，
        # 只剩大字名牌。用高阈值（0.85）认名，避免列表页 OCR 噪声误判。
        name, _r = self._match_name(line)
        if name:
            thr = self.cfg["ocr"].get("match_threshold", 0.7)
            best = max((similarity(line, op["name"]) for op in self.data.operators), default=0)
            if best >= max(0.85, thr):
                log.debug("按名牌高置信命中判定为出货卡: %r → %s (%.2f)", line, name, best)
                return True
        return False

    def on_lobby(self) -> bool:
        """是否停在大厅（右边那一列图标里有「RECRUITMENT / 公开招募」）。

        2026-11-03 实测：宽区域单行 rec 在多行图标上会读成乱码（'心RE'），
        所以改成只读那一行英文标题 + 下面那行中文，两个命中一个就算。
        """
        img = self._frame()
        en = self._rec({"x1": 0.72, "y1": 0.604, "x2": 0.86, "y2": 0.642}, img)
        if "RECRUIT" in en.upper():
            return True
        cn = self._rec({"x1": 0.70, "y1": 0.668, "x2": 0.82, "y2": 0.710}, img)
        if "公开招募" in cn:
            return True
        wide = self._rec({"x1": 0.70, "y1": 0.66, "x2": 0.88, "y2": 0.76}, img)
        return ("公开招募" in wide) or ("RECRUIT" in wide.upper())

    def on_recruit_list(self) -> bool:
        """当前是否真的停在公招列表页（不是大厅、不是基建、不是词条面板）。

        这是「切到基建」bug 的根因守卫：公招列表第 4 格的坐标 (0.746,0.864)
        在大厅/基建页面上正好压在「基建」按钮上，不校验就会一路点进基建。
        具体判定交给 screen_kind()（多信号 + 读不到不造假）。
        """
        return self.screen_kind() == "list"

    def on_base_screen(self) -> bool:
        """是否误入了基建（进驻总览 / 控制中枢 / 建造模式 / 会客室）。

        用三块**单行**小裁切：进驻总览（左上）、控制中枢（中部标题）、建造模式 O”。
        """
        img = self._frame()
        for band, kws in (
            ({"x1": 0.018, "y1": 0.130, "x2": 0.140, "y2": 0.200},
             ("进驻总览",)),
            ({"x1": 0.57, "y1": 0.130, "x2": 0.69, "y2": 0.188},
             ("控制中枢",)),
            ({"x1": 0.020, "y1": 0.255, "x2": 0.190, "y2": 0.320},
             ("建造模式", "无人机")),
        ):
            txt = self._rec(band, img)
            if any(k in txt for k in kws):
                return True
        # 慢速傅底：真的在 1600x900 以下/裁切偏了，就别把基建当成公招页
        txt = self._rec({"x1": 0.02, "y1": 0.10, "x2": 0.55, "y2": 0.35}, img)
        return any(k in txt for k in ("进驻总览", "控制中枢", "建造模式",
                                      "会客室", "电力", "无人机"))

    def leave_base(self) -> bool:
        """从基建里退出来（左上返回 → 确认离开），回到大厅。"""
        log.info("检测到基建界面 → 退出")
        self.adb.tap(0.05 * self.width, 0.05 * self.height)
        time.sleep(0.8)
        self._last_img = None
        img = self.shot()
        dial = self._dialog_text(img, deep=True)
        if "基建" in dial or "是否" in dial:
            # 「是否确认离开罗德岛基建?」→ 点 ✓
            self.adb.tap(0.654 * self.width, 0.690 * self.height)
            time.sleep(1.2)
        self._last_img = None
        return not self.on_base_screen()

    def _sync_to_list(self, tries: int = 3) -> str:
        """把界面拨回公招列表页（只关面板/弹窗，不导航）。返回最终 screen_kind()。

        实机踩坑：面板关闭有动画，点一次 ✗ 后 0.3s 内还会读到「panel」，
        以前就判成「不在列表页」直接放弃，现在重试几拍再下结论。
        """
        kind = self.screen_kind()
        for _ in range(max(1, tries)):
            if kind == "list":
                return kind
            if kind == "panel":
                self.cancel_panel()
            elif kind == "dialog":
                self.dismiss_dialog()
            elif kind == "result":
                self.close_result_fast()
            elif kind == "anim":
                log.info("黑箱动画卡着 → 点 SKIP")
                sk = self.cfg["ui"].get("skip_btn") or {"x": 0.956, "y": 0.073}
                self.adb.tap(sk["x"] * self.width, sk["y"] * self.height)
            else:
                return kind
            time.sleep(self.poll)
            self._last_img = None
            kind = self.screen_kind()
        return kind

    def finish_panel(self) -> str:
        """挂单提交后的收尾：吃掉可能的二次确认 → 把「招募中」面板关掉。

        实机（2026-11-04 23:18）：点面板 ✓ 后，游戏**不会**关面板，而是把同一个
        面板切成「剩余时间/停止招募/立即招募」的招募中视图。以前不收尾，下一格
        就以为「面板还开着」，白花一次自愈。这里顺手 ✗ 掉，返回最终页面类型。
        """
        ok = self.cfg["ui"].get("confirm_ok") or {"x": 0.754, "y": 0.704}
        cancel = self.cfg["ui"].get("cancel_btn") or {"x": 0.753, "y": 0.902}
        for _ in range(3):
            self._last_img = None
            k = self.screen_kind()
            if k == "dialog":
                log.info("挂单出现二次确认 → 点 ✓")
                self.adb.tap(ok["x"] * self.width, ok["y"] * self.height)
                time.sleep(0.6)
                continue
            if k == "panel":
                self.adb.tap(cancel["x"] * self.width, cancel["y"] * self.height)
                time.sleep(0.35)
                continue
            break
        self._last_img = None
        return self.screen_kind()

    def locate_cards(self) -> dict:
        """OCR 列表页，按**实际像素**定位每一格卡片和卡上的按钮。

        为什么需要：PC 客户端的列表布局和 MuMu 上标定的比例不一样（实测 PC 是
        两行卡片分别落在 y≈0.40 / 0.71，而 config 里写的是 0.480 / 0.864），
        于是第 3、4 格点上去会落到卡片外面 —— 表现就是「点立即招募结果点到取消」。
        启动时整页 OCR 一次，把每格卡片中心与按钮中心算出来，之后一律按实际位置点。

        返回 {slot: {"center": (x, y), "button": (x, y) or None}}
        """
        try:
            img = self._frame() if self._last_img is not None else self.shot()
            texts = self.ocr.recognize(img)
        except Exception as e:
            log.debug("卡片定位 OCR 失败: %s", e)
            return {}
        slots_cfg = self.cfg["ui"].get("slots") or []
        if not slots_cfg or not texts:
            return {}
        h, w = img.shape[:2]
        kws = ("开始招募", "立即招募", "聘用候选人", "剩余", "解锁", "招募中", "停止")
        groups = {i: [] for i in range(1, len(slots_cfg) + 1)}
        for t in texts:
            if not any(k in t.text for k in kws):
                continue
            cx, cy = t.center
            if len(slots_cfg) == 4:
                # 2x2 布局：按屏幕中心分四个象限，比「离配置坐标最近」更抗布局漂移
                col = 0 if cx < w / 2 else 1
                row = 0 if cy < h / 2 else 1
                i = row * 2 + col + 1
            else:
                i = min(groups, key=lambda k: (cx - slots_cfg[k - 1]["x"] * w) ** 2
                                            + (cy - slots_cfg[k - 1]["y"] * h) ** 2)
            groups[i].append((t.text, cx, cy))
        out = {}
        for i, items in groups.items():
            if not items:
                continue
            cx = sum(x for _, x, _ in items) / len(items)
            cy = sum(y for _, _, y in items) / len(items)
            cfg_pt = slots_cfg[i - 1]
            # 合理性闸门：平均中心离配置位置超过 0.18 屏宽/高，说明这些文字压根
            # 不是这一格的（实测 2026-11-08：1 号位的文字被算进 3 号格 (279,464)，
            # 于是后面每次 card_center/card_button 都歪，最终点到了别的按钮）。
            if abs(cx - cfg_pt["x"] * w) > 0.18 * w or \
                    abs(cy - cfg_pt["y"] * h) > 0.18 * h:
                log.warning("卡片定位异常：%d 号格算到 (%d,%d)，离配置坐标太远 → 丢弃",
                            i, round(cx), round(cy))
                continue
            btn = None
            for txt, x, y in items:
                if any(k in txt for k in ("立即招募", "聘用候选人")):
                    # 按钮文字必须在这一格里（否则跨列借用 button 也会点歪）
                    if abs(x - cx) <= 0.20 * w:
                        btn = (x, y)
            out[i] = {"center": (cx, cy), "button": btn}
        if out:
            log.info("卡片定位: %s", {k: (round(v["center"][0]), round(v["center"][1]))
                                     for k, v in out.items()})
        return out

    def card_center(self, slot: int):
        """第 slot 格的**实际**点击点：优先用 OCR 定位到的位置，否则回退到配置比例。"""
        m = self.card_map.get(slot)
        if m:
            return m["center"]
        s = (self.cfg["ui"].get("slots") or [])[slot - 1]
        return s["x"] * self.width, s["y"] * self.height

    def card_button(self, slot: int):
        """第 slot 格卡上按钮的实际位置。

        优先用 OCR 直接读到的「立即招募 / 聘用候选人」文字中心；
        没读到（卡片当前是空格子/动画）就用「卡片实际中心 + 配置里的相对偏移」推算，
        这样 PC 版布局和标定不一致时也不会点到卡片外面去。
        """
        m = self.card_map.get(slot) or {}
        if m.get("button"):
            return m["button"]
        cx, cy = self.card_center(slot)
        slots_cfg = self.cfg["ui"].get("slots") or []
        cfg_card = self.cfg["ui"]["fast"]["cards"][slot - 1]["expedite"]
        if slot - 1 < len(slots_cfg):
            ox = cfg_card["x"] - slots_cfg[slot - 1]["x"]
            oy = cfg_card["y"] - slots_cfg[slot - 1]["y"]
        else:
            ox = oy = 0.0
        return cx + ox * self.width, cy + oy * self.height

    def detect_locked_slots(self) -> set:
        """读列表页，找出没解锁的槽位（人力办公室等级不够）。

        认的是卡片上的「…后解锁」提示文字，按坐标把文字归到最近的槽位格子里，
        所以不依赖具体文案/语言，也不依赖格子是不是 2x2 布局。
        返回 {槽位号, ...}；整页 OCR 一次（4~6s），只在启动时跑一次。
        """
        try:
            img = self._frame() if self._last_img is not None else self.shot()
            texts = self.ocr.recognize(img)
        except Exception as e:
            log.debug("未解锁槽位识别失败: %s", e)
            return set()
        slots = self.cfg["ui"].get("slots") or []
        if not slots:
            return set()
        h, w = img.shape[:2]
        locked = set()
        for t in texts:
            if "解锁" not in t.text:
                continue
            cx, cy = t.center
            best, best_d = None, 1e9
            for i, s in enumerate(slots, 1):
                dx = abs(cx - s["x"] * w)
                dy = abs(cy - s["y"] * h)
                d = dx ** 2 + dy ** 2
                if d < best_d:
                    best, best_d = i, d
            # 只认「确实落在某个格子里」的（阈值按格子大小取 0.5 屏宽/高的平方量级）
            if best and best_d < (0.32 * w) ** 2:
                locked.add(best)
        return locked

    def open_slot_fast(self, i: int) -> bool:
        """点开第 i 个公招位。**在大厅/基建/出货卡上绝不点**（否则会点进基建）。

        跟之前的区别：只有「认出来不是公招页」才拒绝；认不出来（other）也允许点
        —— 公招页抬头 OCR 偶尔会失败，以前这种误判会触发一连串自愈把游戏退回桌面。
        点了之后 place_slot 会看 panel_state 决定下一步。
        """
        kind = self._sync_to_list(tries=3)
        if kind in ("lobby", "base", "result"):
            log.warning("当前是 %s 界面 → 拒绝点第 %d 格（防止误入基建）", kind, i)
            self._last_img = None
            return False
        x, y = self.card_center(i)
        self.adb.tap(x, y)
        time.sleep(0.30)
        self._last_img = None
        return True

    def panel_state_fast(self):
        """(状态, 当前时长分钟)。状态: placing / running / other。

        2026-11-04 实机补充：
        - 「招募中」和「待领取」的面板都没有「招募时限」控件，只有职位需求 + 剩余时间
          + 停止招募/立即招募（或 聘用候选人）→ 一律按 running 处理，交回领取阶段；
        - 「招募时限」在就一定是可挂单的面板（空位新开的面板）。
        """
        img = self._frame()
        lab = self._rec(self.cfg["ui"]["fast"]["label"], img)
        cur = self._parse_minutes(self._quick_line("time", img))
        if "招募时限" in lab:
            self._fast_time = cur
            return "placing", cur
        head = self._quick_line("header", img)
        if lab or any(k in head for k in ("剩余", "立即", "已找到", "聘")):
            return "running", None
        dial = self._dialog_text(img)
        if any(k in dial for k in ("是否", "确定", "取消", "不足", "上限", "已用完")):
            return "dialog", None
        return "other", None

    def read_tags_fast(self) -> list:
        """按固定 2×3 网格逐格 rec（6 次 ≈ 0.3s），返回命中词条并记住点击坐标。"""
        grid = self.cfg["ui"]["fast"]["tag_grid"]
        img = self._frame()
        h, w = img.shape[:2]
        thr = self.cfg["ocr"].get("match_threshold", 0.7)
        self._tag_hits = {}
        found, raw = [], []
        for (y1, y2) in grid["rows"]:
            for (x1, x2) in grid["cols"]:
                crop = img[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
                txt = self.fastocr.line(crop).strip()
                raw.append(txt)
                tag = match_tag(txt, threshold=thr) if txt else None
                if tag:
                    self._tag_hits[tag] = ((x1 + x2) / 2 * w, (y1 + y2) / 2 * h)
                    if tag not in found:
                        found.append(tag)
        log.debug("极速词条: %s ← %s", found, raw)
        return found

    def _tag_positions(self) -> dict:
        """用带检测的 OCR 定位词条文字的**真实中心** → {tag: (cx, cy, box)}，整屏像素。

        为什么不直接点 config 里那套 2×3 网格中心：
        网格中心是「格子」的中心，不是「胶囊按钮」的中心；PC 客户端的词条面板和模拟器
        标定值本来就有偏差（卡片布局实测差 100~300px）。点偏了不会报错，日志照样写
        「决策: 召唤」，游戏里却一个都没选上 —— 这就是 2026-11-08 那次的现象。
        带检测 OCR 一次 ≈1s，只在真的要选词条时才跑（多数格子一个都不选）。
        """
        node = (self.cfg["ui"].get("fast") or {}).get("tag_area") or             {"x1": 0.26, "y1": 0.47, "x2": 0.72, "y2": 0.70}
        img = self.shot()
        h, w = img.shape[:2]
        ox, oy = int(node["x1"] * w), int(node["y1"] * h)
        crop = img[oy:int(node["y2"] * h), ox:int(node["x2"] * w)]
        thr = self.cfg["ocr"].get("match_threshold", 0.7)
        out = {}
        try:
            texts = self.ocr.recognize(crop)
        except Exception as e:
            log.debug("词条位置 OCR 失败: %s", e)
            return out
        for t in texts:
            tag = match_tag(t.text, threshold=thr)
            if not tag or tag in out or not t.box:
                continue
            xs = [p[0] + ox for p in t.box]
            ys = [p[1] + oy for p in t.box]
            out[tag] = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2,
                        (min(xs), min(ys), max(xs), max(ys)))
        if out:
            log.info("词条位置: %s", {k: (round(v[0]), round(v[1])) for k, v in out.items()})
        return out

    def _region_changed(self, before, box, min_diff: float = 2.0) -> bool:
        """点击前后同一小块有没有变化（词条选中会变蓝/加描边）。"""
        if before is None or not box:
            return True
        try:
            cur = self._frame()
            x1, y1, x2, y2 = [int(v) for v in box]
            a = before[y1:y2, x1:x2].astype("int16")
            b = cur[y1:y2, x1:x2].astype("int16")
            if a.shape != b.shape or a.size == 0:
                return True
            import numpy as np
            return float(np.abs(a - b).mean()) >= min_diff
        except Exception:
            return True

    def select_tags_fast(self, combo) -> bool:
        """选词条：按 OCR 真实文字中心点，并**逐条验证是否真的选上**。

        返回 False 表示「有词条没能确认选上」——上层会打警告，日志就不会再骗人。
        """
        if not combo:
            return True
        pos = self._tag_positions()
        delay = max(0.12, float(self.cfg["ui"].get("tag_tap_delay", 0.12) or 0))
        all_ok, miss = True, []
        for tag in combo:
            p = pos.get(tag)
            if p is None:                       # 带检测没定位到 → 退回极速网格中心
                hit = self._tag_hits.get(tag)
                if hit is None:
                    miss.append(tag)
                    all_ok = False
                    continue
                p = (hit[0], hit[1], None)
            ok = False
            for attempt in (1, 2):
                before = self._frame()
                self.adb.tap(p[0], p[1])
                log.info("点词条「%s」第 %d 次 @(%.0f,%.0f)", tag, attempt, p[0], p[1])
                time.sleep(delay)
                self._last_img = None
                if self._region_changed(before, p[2]):
                    ok = True
                    break
                log.warning("词条「%s」点了没高亮 → 重试（坐标可能偏了）", tag)
            if not ok:
                all_ok = False
                log.error("词条「%s」两次都没选上（点 @%.0f,%.0f 无效）→ 请重标 tag_area",
                          tag, p[0], p[1])
        if miss:
            log.warning("这些词条没定位到，走慢速路径选: %s", miss)
            try:
                self.select_tags(miss)
            except Exception as e:
                log.debug("慢速选词条失败: %s", e)
        return all_ok

    def set_time_fast(self, hours: int = 9) -> bool:
        """连点「+」到目标时长，最后只做 1 次 rec 校验（约 60ms）。"""
        target = int(hours) * 60
        u = self.cfg["ui"]
        step = u.get("time_step_min", 60)
        cur = self._fast_time if self._fast_time is not None else 60
        if cur >= target:
            return True
        need = max(0, (target - cur) // step)
        px = (u["time_plus"]["x"] * self.width, u["time_plus"]["y"] * self.height)
        # 一次 adb 往返把「+」点完（8 下 ≈0.5s，实测 20ms 间隔也 100% 命中）
        # 注意：时长上限就是 9:00，点过头会绕回 1:00，所以 need=0 时一下都不许点
        if need:
            self.adb.tap_many([px] * min(need, u.get("time_taps_max", 70)),
                              gap_ms=int(u.get("tap_time_gap_ms", 20)))
        if need and not self._time_checked:
            # 本轮第一格才校验（60ms rec + 1 帧）：连点行为稳定，没必要每格都验
            for _ in range(3):
                self._last_img = None
                now = self._parse_minutes(self._quick_line("time"))
                if now is None or now == target:
                    self._fast_time = target
                    return True
                step_left = (target - now) // step
                if step_left <= 0:
                    log.warning("时长点到 %s（绕圈了），改用慢速校准", now)
                    return self.set_time(hours)
                self.adb.tap_many([px] * min(step_left, u.get("time_taps_max", 70)),
                                  gap_ms=int(u.get("tap_time_gap_ms", 20)))
                time.sleep(0.25)
            self._time_checked = True
        self._fast_time = target
        return True

    def confirm_fast(self, verify: bool = True) -> bool:
        """点面板的 ✓ 开始招募（坐标，不 OCR）。

        以前这里会用 OCR 找「确认」按钮、再循环两遍（每遍 4~6s），实测是耗时大户；
        真机上 ✓ 就是提交，点完面板直接关，不需要二次确认。
        命中不了也没关系 —— 下一轮的 panel_state 会立刻发现「还在面板上」。
        """
        node = self.cfg["ui"]["confirm_btn"]
        ok = self.cfg["ui"].get("confirm_ok")
        if not verify:
            self.adb.tap(node["x"] * self.width, node["y"] * self.height)
            if ok:
                self.adb.tap(ok["x"] * self.width, ok["y"] * self.height)
            time.sleep(0.30)
            self._last_img = None
            return True
        for attempt in range(2):
            self.adb.tap(node["x"] * self.width, node["y"] * self.height)
            time.sleep(0.40)
            if ok and attempt == 0:      # 某些版本会弹二次确认，配了就顺手点掉
                self.adb.tap(ok["x"] * self.width, ok["y"] * self.height)
                time.sleep(0.25)
            self._last_img = None
            # 校验：面板上的时间控件消失 = 招募已提交
            if self._parse_minutes(self._quick_line("time")) is None:
                self._last_img = None
                return True
            log.warning("点了 ✓ 但面板还在（第 %d 次）", attempt + 1)
        self._last_img = None
        return False

    def _card_button(self, slot: int) -> str:
        """读第 slot 格卡片上那个按钮是什么。返回 expedite / claim / empty。

        实机（2026-11-04）：无检测的单行 rec 会把「聘用候选人」读成「选人」，
        旧代码据此判「空格子」，白丢一次领取机会（还会把待领卡留在场上）。
        所以单行读数不含任何关键字时，用带检测的 OCR 把卡片区域再看一眼。
        """
        bx, by = self.card_button(slot)
        exp = {"x": bx / self.width, "y": by / self.height}
        band = {"x1": exp["x"] - 0.050, "x2": exp["x"] + 0.050,
                "y1": exp["y"] - 0.030, "y2": exp["y"] + 0.030}
        btn = self._rec(band)
        if "聘用" in btn:
            return "claim"
        if "立即" in btn:              # 只认「立即招募」；「开始招募干员」是空格子
            return "expedite"
        if "开始" in btn:              # 空格子（开始招募干员），不用再复核
            return "empty"
        area = {"x1": exp["x"] - 0.13, "y1": exp["y"] - 0.055,
                "x2": exp["x"] + 0.13, "y2": exp["y"] + 0.055}
        try:
            txt = " ".join(self.ocr.texts(self._crop(area)))
        except Exception as e:
            log.debug("卡片复核 OCR 失败: %s", e)
            txt = ""
        if "聘用" in txt:
            log.info("第 %d 格单行读数不可靠（%r）→ 复核为「聘用候选人」", slot, btn)
            return "claim"
        if "立即" in txt:
            log.info("第 %d 格单行读数不可靠（%r）→ 复核为「立即招募」", slot, btn)
            return "expedite"
        log.info("第 %d 格确认是空格子（单行 %r / 检测 %r）", slot, btn, txt)
        return "empty"

    def claim_card_fast(self, slot: int) -> str:
        """把一格领下来：立即招募（加急）→ 聘用候选人 → 黑箱动画 SKIP → 出货卡。

        返回状态：
          "ok"          已停在出货卡上（可以读名字了）
          "empty"       这格是空的 → 已跳过
          "no_expedite" 加急许可不足 → 已关掉弹窗
          "notlist"     不在公招列表页（上层只做轻量导航）
          "unknown"     8s 内没走到出货卡 → 下轮再来

        2026-11-04 实机重写：以前的固定节奏（点完等 0.5s 再点）几乎必挂——
        加急后卡片要 1~2s 才变成「聘用候选人」，点早了被吞；黑箱动画又必须按 SKIP。
        现在改成**状态机轮询**：每一拍看屏幕上是什么，再决定点哪儿。
        """
        self._close_taps = 0             # 每次领取重新计数（关卡最多点 result_close_taps 次）
        u = self.cfg["ui"]
        w, h = self.width, self.height
        # 按钮位置一律按「OCR 定位到的实际像素」，不再用配置里的死比例
        #（PC 客户端第 3/4 格的布局和模拟器不同，写死会把「立即招募」点成「取消」）
        bx, by = self.card_button(slot)
        cx = self.card_center(slot)[0]     # 卡片中线 = 「聘用候选人」文字中心（实测）
        exp = {"x": bx / w, "y": by / h}
        sk_pt = u["skip_btn"]
        # 读带必须**横向覆盖整条按钮**：加急按钮「立即招募」靠卡片右半格（实测 x≈0.350），
        # 领人按钮「聘用候选人」居中（实测 x≈0.2775）—— 两者中心差 0.07~0.12 屏宽。
        # 2026-11-08 实测（shot_142605_claim3_n1_other.png）：±0.05 的窄带把
        # 「聘用候选人」裁成 '人' → 关键字不命中 → 状态机一次都不点，券白花。
        band = {"x1": min(exp["x"], cx / w) - 0.10,
                "x2": max(exp["x"], cx / w) + 0.10,
                "y1": exp["y"] - 0.030, "y2": exp["y"] + 0.030}

        kind = self._sync_to_list(tries=3)
        if kind != "list":
            log.warning("领第 %d 位前界面是 %s（不是公招列表）→ 不做动作", slot, kind)
            self._last_img = None
            return "notlist"

        what = self._card_button(slot)
        if what == "empty":
            self._last_img = None
            return "empty"
        if what == "claim":
            # _card_button 已经复核出「聘用候选人」→ 直接点卡片中线，别让状态机靠
            # 读带去猜（2026-11-08：窄带读数只有 '人'，白等到 13s 超时后判 unknown，
            # 面板上就留下「已找到候选人」的卡没人领）。
            log.info("第 %d 位卡上已是「聘用候选人」→ 直接点 @(%.0f,%.0f)", slot, cx, by)
            self.adb.tap(cx, by)
            time.sleep(self.poll)
            self._last_img = None
        if what == "expedite":
            log.info("第 %d 位点「立即招募」@(%.0f,%.0f)", slot, bx, by)
            self.adb.tap(bx, by)
            got = self._wait_expedite_dialog(timeout=3.0)
            if got == "no_expedite":
                return "no_expedite"
            if got == "empty":
                return "empty"
            if got == "stopped":
                return "stopped"
            if got == "timeout":
                # 有的版本不弹确认框，直接开加急 → 交给下面的状态机判断
                log.info("点「立即招募」没等到弹窗 → 继续按屏幕状态推进")

        # 状态机：聘用候选人 → 动画 SKIP / 开干员包快进 → 出货卡
        t_end = time.time() + 13.0
        last_log = 0.0
        misses = 0
        nudges = 0
        list_hits = 0
        exp_retry = 0                     # 「立即招募」补点次数上限，防止连点烧券
        while time.time() < t_end:
            self._last_img = None
            if self.on_result_card():
                time.sleep(0.25)
                self._last_img = None
                return "ok"
            b = self._rec(band)
            if any(k in b for k in ("聘", "选", "候选")):
                self.adb.tap(cx, by)          # 居中按钮：点卡片中线最稳
                if time.time() - last_log > 1.5:
                    log.info("第 %d 位点「聘用候选人」", slot)
                    last_log = time.time()
                misses = 0
                time.sleep(self.poll)
                continue
            if "立即" in b:
                # 加急那一下没落上（弹窗超时/坐标偏了）→ 补点，别干等 13s。
                # 最多补 2 次：连点会反复弹确认框，弹窗里的按钮位置和卡片不同，
                # 点多了容易误触（实测 2026-11-08 就把「停止招募」点出来过）。
                exp_retry += 1
                if exp_retry <= 2:
                    log.info("第 %d 位「立即招募」还在 → 补点第 %d 次", slot, exp_retry)
                    self.adb.tap(bx, by)
                    time.sleep(self.poll)
                misses = 0
                if exp_retry <= 2:
                    continue
            sk = self._skip_band_text()
            if "SKIP" in sk.upper() or "跳过" in sk:
                self.adb.tap(sk_pt["x"] * w, sk_pt["y"] * h)
                misses = 0
                time.sleep(self.poll)
                continue

            # 认不出状态：很可能就是「开干员包」那一屏（没文字、没 SKIP）
            # → 连两拍对不上就推一下（点中央 = 快进），否则以前会干等 9s 判失败
            misses += 1
            if misses >= 2:
                kind = self.screen_kind()
                if kind == "list":
                    # 「在列表页」≠「这张卡没了」：已找到候选人时列表页永远判 list，
                    # 卡片上还挂着「聘用候选人」。只有卡上确实没按钮了才认「卡没了」，
                    # 否则继续处理（2026-11-08：旧逻辑 2 拍就 return unknown，券白花）。
                    still = self._card_button(slot)
                    if still == "claim":
                        self.adb.tap(cx, by)
                        misses = 0
                        time.sleep(self.poll)
                        continue
                    if still == "expedite":
                        self.adb.tap(bx, by)
                        misses = 0
                        time.sleep(self.poll)
                        continue
                    list_hits += 1
                    if list_hits >= 2:
                        log.warning("第 %d 位 已经回到列表页（卡没了）→ 不再空点", slot)
                        return "unknown"
                    misses = 0
                    time.sleep(self.poll)
                    continue
                list_hits = 0
                limit = int(self.cfg["ui"].get("claim_nudges", 4))
                if nudges < 3:
                    try:
                        self.save_shot(f"claim{slot}_n{nudges + 1}_{kind}")
                    except Exception:
                        pass
                if nudges < limit:
                    how = self.advance_result(deep=(nudges % 3 == 2))
                    nudges += 1
                    log.info("第 %d 位 状态没进展（kind=%s）→ 快进(%s) 第 %d/%d 次",
                             slot, kind, how, nudges, limit)
                misses = 0
                time.sleep(self.poll * 1.2)
                continue
            time.sleep(self.poll)
        log.warning("第 %d 位 13s 内没走到出货卡（卡在 %s，快进 %d 次）",
                    slot, self.screen_kind(), nudges)
        try:
            self.save_shot(f"slot{slot}_stuck")
        except Exception:
            pass
        self._last_img = None
        return "unknown"

    def _wait_expedite_dialog(self, timeout: float = 2.6) -> str:
        """轮询等「是否消耗一张加急许可」弹窗。返回 ok / no_expedite / empty / timeout。"""
        u = self.cfg["ui"]
        end = time.time() + timeout
        deep_turn = 0
        while time.time() < end:
            time.sleep(0.20)
            self._last_img = None
            try:
                img = self.shot()
            except Exception as e:
                log.warning("等弹窗时截图失败: %s", e)
                continue
            dial = self._dialog_text(img, deep=(deep_turn == 1))
            deep_turn ^= 1
            # ★ 最危险的一屏：这根本不是加急确认，而是「停止招募将放弃已投入的资源」。
            # 它的确认点在右侧红底（实测文字框 x 0.62~0.72），而 ui.expedite_confirm
            # 默认就在 (0.754, 0.704) —— 正好落在「确认停止」按钮里。
            # 2026-11-08 实机：脚本把已挂好的招募直接停掉（截图见对话）。
            # 所以这里必须**先**认出它，并且只点左侧「取消」。
            if "停止" in dial or "放弃" in dial:
                log.warning("这是「停止招募」确认框，不是加急确认 → 点「取消」")
                self.adb.tap(0.23 * self.width, 0.646 * self.height)
                time.sleep(0.25)
                self._last_img = None
                return "stopped"
            if "加急" in dial and any(k in dial for k in ("不足", "没有", "无法", "不够")):
                log.warning("加急许可不足（%s）", dial)
                self.adb.tap(0.23 * self.width, 0.70 * self.height)
                time.sleep(0.25)
                self._last_img = None
                return "no_expedite"
            if any(k in dial for k in ("是否", "确定", "加急", "冷静", "确认")):
                ec = u.get("expedite_confirm") or {"x": 0.754, "y": 0.704}
                self.adb.tap(ec["x"] * self.width, ec["y"] * self.height)
                time.sleep(0.45)
                self._last_img = None
                return "ok"
            if self.on_result_card():
                return "ok"                # 有的版本不弹窗，直接进结果
            if self._parse_minutes(self._rec(u["fast"]["time"], img)) is not None:
                log.info("点开的是空位的招募面板 → 关掉")
                node = u.get("cancel_btn") or {"x": 0.753, "y": 0.902}
                self.adb.tap(node["x"] * self.width, node["y"] * self.height)
                time.sleep(0.30)
                self._last_img = None
                return "empty"
        return "timeout"

    def links_left(self):
        """今日剩余人脉联络次数（顶部「联络次数 X/3」）。

        注意：它是「人脉联络/刷新标签」的额度，**和能不能开公招位无关**
        （实测 0/3 照样能招募），只用来打日志。
        """
        txt = self.fastocr.line(self._crop({"x1": 0.60, "y1": 0.09,
                                            "x2": 0.80, "y2": 0.14}))
        m = re.search(r"(\d)\s*/\s*3", txt or "")
        if m:
            return int(m.group(1))
        m = re.search(r"(\d)", txt or "")
        return int(m.group(1)) if m else None

    def budget_left(self):
        """面板上「招募预算 N/1」里的 N —— 真正限制能不能开位的资源。"""
        txt = self.fastocr.line(self._crop({"x1": 0.27, "y1": 0.90,
                                            "x2": 0.44, "y2": 0.97}))
        m = re.search(r"(\d+)\s*/\s*\d+", txt or "")
        return int(m.group(1)) if m else None

    def kill_popups_fast(self, rounds: int = 3) -> int:
        """把挡住流程的弹窗一律点掉（公告/联络次数/加急确认/奖励）。

        "一直弹窗"的止血点：每个弹窗都在 2 次 60ms 的 rec 内被识别并关掉。
        """
        closed = 0
        for _ in range(rounds):
            img = self.shot()
            dial = self._dialog_text(img)
            if not dial:
                self._last_img = img
                break
            if any(k in dial for k in ("是否", "确定", "取消", "不足", "上限", "耗尽")):
                log.info("关弹窗: %s", dial)
                self.adb.tap(0.23 * self.width, 0.70 * self.height)   # ✗
                closed += 1
                time.sleep(0.25)
                self._last_img = None
                continue
            self._last_img = img
            break
        return closed

    def dismiss_dialog(self) -> bool:
        """关掉残留的确认弹窗（✗ 在弹窗左下）。返回是否关掉了。"""
        img = self.shot()
        dial = self._dialog_text(img)
        # 「停止招募」确认框也归这里关（点左侧取消那一下，绝不能点右边确认）。
        if any(k in dial for k in ("是否", "加急", "联络", "不足", "确定", "停止", "放弃")):
            self.adb.tap(0.23 * self.width, 0.70 * self.height)
            time.sleep(0.25)
            self._last_img = None
            log.info("关掉残留弹窗: %s", dial)
            return True
        self._last_img = None
        return False

    def _match_name(self, txt: str):
        """把一段文字匹配到干员名 → (name, rarity)；匹配不上 (None, 0)。"""
        thr = self.cfg["ocr"].get("match_threshold", 0.7)
        if not txt:
            return None, 0
        best, score, rar = None, 0.0, 0
        for op in self.data.operators:
            sc = similarity(txt, op["name"])
            if sc > score:
                best, score, rar = op["name"], sc, op["rarity"]
        if best and score >= thr:
            return best, rar
        for op in self.data.operators:      # 单行 rec 常把前后缀一起读进来
            if op["name"] in txt:
                return op["name"], op["rarity"]
        return None, 0

    def _name_from_reward(self, txt: str):
        """从奖励栏「XX的信物」反推干员名 → (name, rarity)。"""
        m = re.search(r"([^\s，。,+\d]{1,6}?)的信物", txt or "")
        if not m:
            return None, 0
        frag = re.sub(r"^[^一-龥]+", "", m.group(1))
        return self._match_name(frag)

    def read_name_fast(self):
        """出货名：中文名条带单行 rec（≈60ms/次，最多试 3 帧）。

        读不到时先判「是不是根本没招募成」（弹出的是联络机会/加急弹窗），
        再退到「名条带 det+rec」，最后才整屏 OCR —— 逐级加价，不一上来就慢。
        """
        for _ in range(2):
            name, rar = self._match_name(self._quick_line("name"))
            if name:
                return name, rar
            self._last_img = None
            time.sleep(0.35)

        # 证据二：右侧奖励栏「XX的信物」反推干员名。
        # 2026-11-08 实测：大字艺术名会被拆成单字（'刻' / '近'），但「刻刀的信物」
        # 这一整行读得出来 —— 重复出货时这条最可靠。
        rew = self._reward_text()
        name, rar = self._name_from_reward(rew)
        if name:
            log.info("从奖励栏读到出货干员: %d★ %s（%s）", rar, name, rew[:24])
            return name, rar

        # 证据三：名牌区域 det+rec 放大认（≈1s），比整屏 OCR 便宜
        try:
            texts = self._ocr_name_region(self.shot())
            name, rar, score = self._match_operator(texts)
            if name and score >= self.cfg["ocr"].get("match_threshold", 0.7):
                log.info("名牌区 det-OCR 读到出货干员: %d★ %s", rar, name)
                return name, rar
        except Exception as e:
            log.debug("名牌区 det-OCR 失败: %s", e)

        # 最后才推快进：点中央会把出货卡**点掉**，所以放在所有只读手段之后
        if self.advance_result(deep=True) != "none":
            time.sleep(0.45)
            self._last_img = None
            name, rar = self._match_name(self._quick_line("name"))
            if name:
                log.info("推快进后读到出货干员: %d★ %s", rar, name)
                return name, rar

        # 诊断：屏幕上是不是还挂着「是否消耗…」的弹窗（说明槽位是空的）
        img = self.shot()
        dial = self._dialog_text(img, deep=True)
        if "加急" in dial and ("不足" in dial or "没有" in dial or "无法" in dial):
            self.claim_diag = "no_expedite"
            log.warning("加急许可不足（%s）→ 转自然招募模式", dial)
            self.adb.tap(0.23 * self.width, 0.70 * self.height)   # ✗
            time.sleep(0.25)
            self._last_img = None
            return None, 0
        if "联络" in dial or "是否" in dial:
            self.claim_diag = "dialog"
            log.warning("领取时还挂着弹窗（%s）→ 这个位其实是空的，本轮跳过", dial)
            self.adb.tap(0.23 * self.width, 0.70 * self.height)   # ✗ 关掉，别污染下一格
            time.sleep(0.25)
            self._last_img = None
            return None, 0

        # 名条带放大后带检测再认一遍（≈1s，比整屏 4~6s 便宜）
        try:
            import cv2
            crop = self._crop(self.cfg["ui"]["fast"]["name"], img)
            big = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
            texts = self.ocr.texts(big)
            for t in texts:
                name, rar = self._match_name(t)
                if name:
                    return name, rar
        except Exception as e:
            log.debug("名条带放大 OCR 失败: %s", e)

        log.warning("极速读出货失败，退回整屏 OCR（最多 4s）")
        try:
            return self.read_result(timeout=4)
        except Exception as e:              # 无人值守：读不到就算了，别把整轮带崩
            log.warning("整屏 OCR 也失败: %s", e)
            return None, 0

    def _wait_change(self, before, timeout: float = None, min_diff: float = 1.5) -> bool:
        """点完之后等界面**真的**变化（帧差 > min_diff），返回是否变了。

        为什么要这个：以前点完固定 sleep(0.2s) 就开始下一轮判断，转场动画没结束 →
        `screen_kind()` 读到 other → 又点一次屏幕中央。而在公招列表页上，
        这两下"补刀"就是纯空点（用户看到的就是"回大厅后空点好几下"）。
        现在点一下等变化，没变化就立刻收手，绝不重复点。
        """
        if self.dry_run:                     # 离线模拟没有像素，别等
            return True
        if timeout is None:
            timeout = float(self.cfg["ui"].get("ui_settle", 0.9))
        try:
            import numpy as np
        except Exception:
            time.sleep(timeout)
            return True
        if before is None or not hasattr(before, "shape"):
            time.sleep(min(timeout, 0.3))
            return True
        end = time.time() + max(0.15, timeout)
        while time.time() < end:
            time.sleep(0.12)
            self._last_img = None
            cur = self._frame()
            try:
                if cur is None or not hasattr(cur, "shape"):
                    return True
                if float(np.abs(before.astype("int16") - cur.astype("int16")).mean()) >= min_diff:
                    return True
            except Exception:
                return True
        self._last_img = None
        return False

    def tap_result_close(self) -> bool:
        """点出货卡左下空白「关卡」。

        次数由 `ui.result_close_taps` 控制（默认 **1 次**）：
        整个领取流程里只允许点这么多次，之后就算还停在卡上也只点中央（快进），
        不会反复戳同一个位置（那是空点，而且会把还没播完的卡点掉/点乱）。
        """
        allow = int(self.cfg["ui"].get("result_close_taps", 1))
        if self._close_taps >= max(0, allow):
            return False
        node = self.cfg["ui"].get("result_close") or {"x": 0.125, "y": 0.889}
        self._close_taps += 1
        self.adb.tap(node["x"] * self.width, node["y"] * self.height)
        log.debug("关卡第 %d/%d 次", self._close_taps, allow)
        return True

    def skip_result_pages(self, max_taps: int = 4, timeout: float = 3.5) -> bool:
        """出货卡 →（干员介绍 / 技能演示等过渡页）→ 公招列表，见缝就快进。

        2026-11-07 实机：抽到干员后不是只有一张出货卡 —— 新干员还会弹「干员介绍」，
        整屏立绘 + 文字，**点一下就能快进**。旧逻辑只点左下空白然后等 4×0.35s，
        遇到介绍页就白等，一格多花 2~4 秒。

        现在的节奏（每一下都先看屏，确认不是列表才点）：
          第 1 下：出货卡 → 点 ui.result_close（卡面左下空白，唯一能关卡的位置）
          第 2 下起：介绍/演示页 → 点屏幕中央（快进）
        回到列表立刻收手，绝不多点（多点的下一格会被误开词条面板）。

        返回 True = 已经回到公招列表。
        """
        u = self.cfg["ui"]
        rp = u.get("result_pages") or {}
        max_taps = int(rp.get("max_taps", max_taps))
        timeout = float(rp.get("timeout", timeout))
        t_end = time.time() + timeout
        for i in range(max_taps):
            self._last_img = None
            kind = self.screen_kind()
            if kind == "list":
                return True
            if kind in ("panel", "lobby", "base"):
                # 已经离开出货流程了（多半是误入面板），别再点中央
                return False
            if time.time() > t_end:
                break
            before = self._frame()
            if self.on_result_card() and self.tap_result_close():
                pass                                    # 关卡（全局最多 result_close_taps 次）
            else:
                log.debug("快进第 %d 下：点屏幕中央（干员介绍/演示页）", i + 1)
                self.adb.tap(0.5 * self.width, 0.5 * self.height)
            if not self._wait_change(before):
                # 点下去界面纹丝不动 → 说明已经没东西可点，别在列表上空点
                log.debug("点了没反应 → 停止补刀")
                return self.screen_kind() == "list"
        self._last_img = None
        return self.screen_kind() == "list"

    def close_result_fast(self) -> bool:
        """关掉出货卡（点卡面左下空白）。返回是否回到公招列表。

        2026-11-04 实机踩坑：以前先点左下、再点右侧 (0.875,0.55)，第二下在**列表页**
        正好落在第 2 格上 → 把第 2 格的词条面板点开，后面的领取全乱。
        现在每次点击前都先确认「真的还在出货卡上」，关掉就立刻收手。
        """
        if self.skip_result_pages():
            return True
        self._last_img = None
        if self.screen_kind() == "list":
            return True
        # 兜底：可能是被点开的面板/弹窗，轻量回收一次
        return self._sync_to_list(tries=2) == "list"
