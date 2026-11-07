"""离线模拟器：不需要 MuMu 也能把整条主流程跑一遍。

用途：
  - `python main.py --dry-run`：用假 ADB + 假 OCR 模拟"4 个公招位 → 选词条 →
    设 9h → 确认 → 加急 → 出货 → 循环"，验证控制流/决策/统计/日志，
    一行 ADB 命令都不发；
  - `tests/test_flow.py`：把控制流固化成回归测试。

它模拟的是**游戏界面状态机**，不是像素：截图返回 SimScreen 对象本身，
FakeOcr 直接从该对象读取"当前屏幕上的文字与坐标"。
"""
import random

from .ocr import OcrText

# 随机出货池（星级 → 名字），只用真实数据里存在的名字避免干扰
POOL = {
    1: ["Lancet-2", "Castle-3"],
    2: ["黑角", "杜林", "12F", "巡林者", "夜刀"],
    3: ["玫兰莎", "芬", "克洛丝", "米格鲁", "炎熔"],
    4: ["宴", "缠丸", "蛇屠箱", "古米", "夜烟"],
    5: ["因陀罗", "火神", "守林人", "诗怀雅"],
    6: ["斯卡蒂", "赫拉格", "煌", "山", "能天使"],
}


class SimScreen:
    """公招界面状态机。"""

    def __init__(self, width=1280, height=720, slots=4, seed=None, empty_slots=None,
                 slot_pos=None, ui=None, time_step_min=10):
        self.width = width
        self.height = height
        self.n_slots = slots
        # 公招位坐标（屏幕比例）；不传就用居中竖排的旧布局。
        # 真机是 2x2 四格（config.ui.slots），dry-run 必须用同一套坐标，
        # 否则 open_slot 点空 → 模拟器永远进不了词条面板。
        self.slot_pos = slot_pos
        # 真机 UI 坐标（config.ui）与「+」步长也要用真的，否则 set_time 点空
        self.ui = ui or {}
        self.time_step = int(time_step_min or 10)
        self.rng = random.Random(seed)
        self.state = "main"
        self.cur = 0
        self.selected = []
        self.taps = 0
        # 槽位：{"status": ready|running, "tags": [...], "minutes": int}
        self.slots = [{"status": "ready", "tags": self._new_tags(), "minutes": 60}
                      for _ in range(slots)]
        if empty_slots:
            for i in empty_slots:
                self.slots[i]["status"] = "empty"
        self._pending = None   # 暂存已确认的槽位
        self._result = None    # (name, rarity)

    # ---------- 词条 ----------
    TAG_POOL = ["近卫干员", "狙击干员", "重装干员", "医疗干员", "辅助干员", "术师干员",
                "特种干员", "先锋干员", "近战位", "远程位", "男性干员", "女性干员",
                "新手", "治疗", "支援", "输出", "生存", "群攻", "防护", "减速",
                "削弱", "控场", "爆发", "快速复活", "位移", "召唤", "支援机械",
                "费用回复", "元素"]

    def _new_tags(self):
        pool = list(self.TAG_POOL)
        self.rng.shuffle(pool)
        tags = pool[:5]
        # 3% 概率塞个 6★ 词条，方便验证保底分支
        if self.rng.random() < 0.03:
            tags[0] = "高级资深干员"
        elif self.rng.random() < 0.08:
            tags[0] = "资深干员"
        return tags

    def ui_xy(self, key, dflt):
        """config.ui 里的比例坐标 → 像素；没配就用兜底值。"""
        node = self.ui.get(key) or {}
        return (self.width * node.get("x", dflt[0]),
                self.height * node.get("y", dflt[1]))

    def slot_xy(self, i):
        if self.slot_pos and i < len(self.slot_pos):
            rx, ry = self.slot_pos[i]
            return (self.width * rx, self.height * ry)
        y = [0.30, 0.45, 0.60, 0.75][i]
        return (self.width * 0.5, self.height * y)

    # ---------- 屏幕文字 ----------
    def texts(self):
        """返回当前屏幕的 [(text, x, y)]。"""
        s = self.state
        out = []
        if s == "main":
            out.append(("终端", 640, 620))
        elif s == "terminal":
            out.append(("公开招募", 320, 300))
            out.append(("终端", 640, 620))
        elif s == "list":
            out.append(("公开招募", 640, 90))
            out.append(("招募说明", 1180, 700))
            for i, sl in enumerate(self.slots):
                x, y = self.slot_xy(i)
                label = {"ready": "开始招募干员", "running": "剩余 08:59:59",
                         "empty": "空闲"}[sl["status"]]
                out.append((label, x, y))
        elif s == "detail":
            sl = self.slots[self.cur]
            out.append(("职业需求", 200, 120))
            out.append(("招募时限", 470, 560))   # 时长读数的锚点，缺了 UI 会拒绝读数
            if sl["status"] == "ready":
                for k, tag in enumerate(sl["tags"]):
                    out.append((tag, 220 + (k % 3) * 260, 260 + (k // 3) * 90))
                out.append((self._fmt_time(sl["minutes"]), 640, 560))
                out.append(("＋", *self.ui_xy("time_plus", (0.594, 0.778))))
                out.append(("－", *self.ui_xy("time_minus", (0.406, 0.778))))
                out.append(("开始招募", *self.ui_xy("confirm_btn", (0.75, 0.9))))
            else:
                out.append((self._fmt_time(sl["minutes"]), 640, 560))
                out.append(("剩余 08:59:59", 400, 480))
                out.append(("加急许可", *self.ui_xy("expedite_btn", (0.32, 0.53))))
        elif s == "confirm":
            out.append(("确认", *self.ui_xy("confirm_btn", (0.75, 0.9))))
        elif s == "expedite_confirm":
            out.append(("确定", *self.ui_xy("expedite_confirm", (0.754, 0.704))))
        elif s == "claim":
            out.append(("已找到符合部分要求的候选人", 640, 300))
            out.append(("聘用候选人", *self.ui_xy("claim_btn", (0.254, 0.529))))
        elif s == "anim":
            # 黑箱装证件动画：整屏只有 SKIP + 部门 logo（真机 2026-11-03 实测）
            out.append(("SKIP", *self.ui_xy("skip_btn", (0.956, 0.073))))
            out.append(("HUMAN", 1180, 430))
            out.append(("RESOURCE", 1200, 470))
            out.append(("DEPARTMENT", 1215, 505))
        elif s == "result":
            name, rarity = self._result
            out.append((name, 640, 280))
            out.append((f"{rarity}★", 640, 360))
            out.append(("确认", 900, 660))
        return out

    @staticmethod
    def _fmt_time(minutes):
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    # ---------- 点击 ----------
    def tap(self, x, y):
        self.taps += 1
        if self.state == "list":
            for i in range(self.n_slots):
                sx, sy = self.slot_xy(i)
                if abs(x - sx) < 90 and abs(y - sy) < 60:
                    self.cur = i
                    self.selected = []
                    self.state = "detail"
                    return
            return
        # 其他状态：找最近的可点文字
        cands = sorted(self.texts(), key=lambda t: (t[1] - x) ** 2 + (t[2] - y) ** 2)
        if not cands:
            return
        text, _, _ = cands[0]
        self._click_text(text)

    def _click_text(self, text):
        s = self.state
        if s == "main" and text == "终端":
            self.state = "terminal"
        elif s == "terminal" and text == "公开招募":
            self.state = "list"
        elif s == "detail":
            sl = self.slots[self.cur]
            if sl["status"] == "ready":
                if text in sl["tags"]:
                    if text in self.selected:
                        self.selected.remove(text)
                    elif len(self.selected) < 5:
                        self.selected.append(text)
                elif text == "＋":
                    sl["minutes"] = min(sl["minutes"] + self.time_step, 1440)
                elif text == "－":
                    sl["minutes"] = max(sl["minutes"] - self.time_step, 10)
                elif text == "开始招募":
                    self._pending = list(self.selected)
                    self.state = "confirm"
            elif text == "加急许可":
                self.state = "expedite_confirm"
        elif s == "confirm" and text == "确认":
            sl = self.slots[self.cur]
            sl["status"] = "running"
            sl["tags"] = list(self._pending or [])
            self.state = "list"
        elif s == "expedite_confirm" and text == "确定":
            self.state = "claim"
        elif s == "claim" and text == "聘用候选人":
            self.state = "anim"
        elif s == "anim" and text == "SKIP":
            self._roll()
            self.state = "result"
        elif s == "result" and text == "确认":
            self._result = None
            self.state = "list"

    def _roll(self):
        sl = self.slots[self.cur]
        tags = set(sl["tags"])
        if "高级资深干员" in tags:
            rarity = 6
        elif "资深干员" in tags:
            rarity = 5
        else:
            rarity = self.rng.choices([3, 4], weights=[0.7, 0.3])[0]
        name = self.rng.choice(POOL[rarity])
        self._result = (name, rarity)
        sl["status"] = "empty"
        sl["tags"] = self._new_tags()

    def back(self):
        if self.state in ("detail", "confirm", "expedite_confirm", "claim", "anim", "result"):
            self.state = "list"
        elif self.state == "list":
            self.state = "terminal"
        elif self.state == "terminal":
            self.state = "main"

    def reset_done_slots(self):
        for sl in self.slots:
            if sl["status"] == "empty":
                sl["status"] = "ready"


class FakeAdb:
    def __init__(self, sim: SimScreen):
        self.sim = sim
        self.serial = "sim:0"
        self.taps = []

    def connect(self):
        return self.serial

    def wm_size(self):
        return self.sim.width, self.sim.height

    def screencap(self):
        return self.sim

    def tap(self, x, y):
        self.taps.append((x, y))
        self.sim.tap(x, y)

    def swipe(self, *a, **k):
        pass

    def long_press(self, *a, **k):
        pass

    def keyevent(self, code):
        pass

    def back(self):
        self.sim.back()

    def current_package(self):
        return "sim.arknights"

    def start_app(self, package, activity=""):
        pass

    def app_running(self, package):
        return True


class FakeOcr:
    """从 SimScreen 直接读文字，绕过真 OCR。"""

    def __init__(self, sim: SimScreen):
        self.sim = sim

    def recognize(self, img=None):
        return [OcrText(t, [[x - 20, y - 10], [x + 20, y - 10],
                            [x + 20, y + 10], [x - 20, y + 10]], 0.99)
                for t, x, y in self.sim.texts()]

    def texts(self, img=None):
        return [t for t, _, _ in self.sim.texts()]

    def find_text(self, img, keyword):
        for t in self.recognize():
            if keyword in t.text or t.text in keyword:
                return t
        return None

    def find_any(self, img, keywords):
        for kw in keywords:
            t = self.find_text(img, kw)
            if t:
                return t
        return None
