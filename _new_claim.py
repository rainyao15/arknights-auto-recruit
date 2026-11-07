    def claim_card_fast(self, slot: int) -> str:
        """卡上三步一气呵成：立即招募 → 弹窗 ✓ → 聘用候选人 → SKIP。

        返回状态：
          "ok"          正常进入出货卡
          "empty"       这格其实是空的（点开的是招募面板）→ 已关掉
          "no_expedite" 加急许可不足 → 已关掉弹窗
          "notlist"     当前不在公招列表页（上层只做轻量导航，别全量自愈）
          "unknown"     点了没反应（弹窗没出/点击没落上）→ 下轮再领

        2026-11-03 踩坑：以前固定等 0.5s 就点 ✓，弹窗常常还没出来（dial=''），
        整轮判失败 → 触发自愈 → 连点返回 → 把游戏按回模拟器桌面。现在改成轮询。
        """
        u = self.cfg["ui"]
        w, h = self.width, self.height
        card = u["fast"]["cards"][slot - 1]
        exp = card["expedite"]
        claim = card["claim"]
        sk = u["skip_btn"]

        kind = self.screen_kind()
        if kind in ("panel", "dialog"):
            if kind == "panel":
                self.cancel_panel()
            else:
                self.dismiss_dialog()
            self._last_img = None
            kind = self.screen_kind()
        if kind != "list":
            log.warning("领第 %d 位前界面是 %s（不是公招列表）→ 不做动作", slot, kind)
            self._last_img = None
            return "notlist"

        # ★ 先确认这一格真的有「立即招募」再点：空位盲点会把游戏带到别的界面
        band = {"x1": exp["x"] - 0.050, "x2": exp["x"] + 0.050,
                "y1": exp["y"] - 0.025, "y2": exp["y"] + 0.025}
        btn = self._rec(band)
        if "聘用" in btn:
            log.info("第 %d 格已有人选 → 直接领", slot)
            self.adb.tap(claim["x"] * w, claim["y"] * h)
            time.sleep(0.55)
            self.adb.tap(sk["x"] * w, sk["y"] * h)
            time.sleep(0.75)
            self._last_img = None
            return "ok"
        if ("立即" not in btn) and ("招募" not in btn):
            log.info("第 %d 格没有「立即招募」（读到 %r）→ 这格是空的，跳过", slot, btn)
            self._last_img = None
            return "empty"

        for attempt in range(2):
            self.adb.tap(exp["x"] * w, exp["y"] * h)
            got = self._wait_expedite_dialog(timeout=2.6)
            if got == "ok":
                self._finish_claim(claim, sk)
                return "ok"
            if got in ("no_expedite", "empty"):
                return got
            log.info("第 %d 次点「立即招募」没等到弹窗 → 确认界面后补点一次", attempt + 1)
            self._last_img = None
            if self.screen_kind() != "list":
                return "unknown"
        self._last_img = None
        return "unknown"

    def _wait_expedite_dialog(self, timeout: float = 2.6) -> str:
        """轮询等「是否消耗一张加急许可」弹窗。返回 ok / no_expedite / empty / timeout。"""
        u = self.cfg["ui"]
        end = time.time() + timeout
        band = {"x1": 0.10, "y1": 0.46, "x2": 0.90, "y2": 0.74}
        while time.time() < end:
            time.sleep(0.20)
            self._last_img = None
            try:
                img = self.shot()
            except Exception as e:
                log.warning("等弹窗时截图失败: %s", e)
                continue
            dial = self._rec(band, img)
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

    def _finish_claim(self, claim_pt, skip_pt) -> None:
        """✓ 之后：点「聘用候选人」→ SKIP 动画。少一步都读不到出货。"""
        w, h = self.width, self.height
        for _ in range(2):
            self.adb.tap(claim_pt["x"] * w, claim_pt["y"] * h)
            time.sleep(0.55)
            self._last_img = None
            if self.on_result_card():
                break
        self.adb.tap(skip_pt["x"] * w, skip_pt["y"] * h)
        time.sleep(0.75)
        self._last_img = None

