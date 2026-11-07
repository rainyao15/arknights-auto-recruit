"""出货统计与历史记录。

记录粒度：每次公招成功出货一行，追加到 `logs/history.jsonl`（不覆盖历史）。
汇总：按星级计数 + 票据折算。

票据折算权重在 config.yaml 的 `stats.ticket_weights` 里配置，默认给出
一套常见口径（3★ 起算），你可按自己服务器的实际收益自行改数字——
脚本不猜你的经济系统，只做加法。
"""
import json
import os
from datetime import datetime

from .logger import get_logger

log = get_logger()

DEFAULT_WEIGHTS = {1: 0, 2: 0, 3: 1, 4: 1, 5: 1, 6: 1}


class Stats:
    def __init__(self, cfg, history_path: str = "logs/history.jsonl"):
        self.cfg = cfg
        self.path = history_path
        st = (cfg.get("stats") or {})
        self.weights = {int(k): int(v) for k, v in (st.get("ticket_weights") or DEFAULT_WEIGHTS).items()}
        self.enabled = st.get("record", True)
        self.session = []          # 本次运行
        self.totals = {}           # 历史累计（从 jsonl 读回）
        self._load_history()

    # ---------- 历史 ----------
    def _load_history(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self._add(self.totals, rec.get("rarity", 0))
        except OSError as e:
            log.warning("读取历史失败: %s", e)

    @staticmethod
    def _add(bucket: dict, rarity: int):
        if not rarity:
            return
        bucket[rarity] = bucket.get(rarity, 0) + 1

    # ---------- 记录 ----------
    def record(self, slot: int, name: str, rarity: int, combo=None, reason: str = ""):
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "slot": slot,
            "name": name or "",
            "rarity": int(rarity or 0),
            "combo": list(combo or []),
            "reason": reason,
        }
        self.session.append(rec)
        self._add(self.totals, rec["rarity"])
        if self.enabled:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except OSError as e:
                log.warning("写历史失败: %s", e)
        return rec

    # ---------- 汇总 ----------
    def tickets(self, bucket: dict) -> int:
        return sum(self.weights.get(r, 0) * c for r, c in bucket.items())

    def session_summary(self) -> str:
        return self._fmt(self.session, "本次")

    def total_summary(self) -> str:
        return self._fmt(None, "累计")

    def _fmt(self, records, title: str) -> str:
        if records is None:
            bucket = self.totals
            n = sum(bucket.values())
        else:
            bucket = {}
            for r in records:
                self._add(bucket, r["rarity"])
            n = len(records)
        if not n:
            return f"{title}：无出货记录"
        stars = "、".join(f"{r}★×{c}" for r, c in sorted(bucket.items(), reverse=True))
        return f"{title} {n} 次 | {stars} | 折合票据≈{self.tickets(bucket)}"

    def best_of_session(self) -> str:
        hits = [r for r in self.session if r.get("rarity")]
        if not hits:
            return ""
        top = max(hits, key=lambda r: r["rarity"])
        return f"{top['rarity']}★ {top['name']}"
