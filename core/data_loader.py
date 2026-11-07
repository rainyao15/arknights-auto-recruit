"""词条/干员数据加载。

数据来源：MAA 的 resource/recruitment.json，结构：
{
  "tags": {"高级资深干员": "高级资深干员", ...},
  "operators": [{"id": "char_xxx", "name": "干员名", "rarity": 1-6, "tags": [...]}, ...]
}
"""
import json
import os

import requests

from .logger import get_logger

log = get_logger()

# 31 个词条兜底常量（即使数据文件缺失也能用）
TAGS_31 = [
    "高级资深干员", "资深干员",
    "近卫干员", "狙击干员", "重装干员", "医疗干员", "辅助干员", "术师干员", "特种干员", "先锋干员",
    "近战位", "远程位", "男性干员", "女性干员",
    "新手", "治疗", "支援", "输出", "生存", "群攻", "防护", "减速", "削弱", "控场", "爆发",
    "快速复活", "位移", "召唤", "支援机械", "费用回复", "元素",
]


class RecruitData:
    def __init__(self, tags: dict, operators: list):
        self.tags = tags or {}
        self.operators = operators or []
        # 按星级分桶，便于决策
        self.by_rarity = {}
        for op in self.operators:
            self.by_rarity.setdefault(op["rarity"], []).append(op)

    def names(self) -> list:
        return [op["name"] for op in self.operators]

    def operators_with_tag(self, tag: str) -> list:
        return [op for op in self.operators if tag in op["tags"]]


class DataLoader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cache_path = cfg["data"]["cache_path"]
        self.source_url = cfg["data"]["source_url"]

    def _download(self) -> dict:
        log.info("拉取最新词条数据: %s", self.source_url)
        r = requests.get(self.source_url, timeout=30)
        r.raise_for_status()
        data = r.json()
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data

    def load(self) -> RecruitData:
        data = None
        if self.cfg["data"]["update_on_start"]:
            try:
                data = self._download()
            except Exception as e:
                log.warning("在线更新失败(%s)，回退本地缓存", e)
        if data is None:
            if not os.path.exists(self.cache_path):
                try:
                    data = self._download()
                except Exception as e:
                    log.error("本地无缓存且下载失败: %s", e)
                    data = {"tags": {}, "operators": []}
            else:
                with open(self.cache_path, encoding="utf-8") as f:
                    data = json.load(f)
        tags = data.get("tags", {})
        operators = data.get("operators", [])
        log.info("词条数据加载完成：%d 个词条，%d 个干员", len(tags), len(operators))
        return RecruitData(tags, operators)
