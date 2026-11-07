"""词条识别：把 OCR 文本匹配到 31 个公招词条白名单。"""
from difflib import SequenceMatcher

from .data_loader import TAGS_31


def _norm(s: str) -> str:
    return "".join(s.split())


def similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 包含关系直接高分
    if a in b or b in a:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def match_tag(text: str, tags=None, threshold: float = 0.7) -> str | None:
    """把一段 OCR 文本匹配到最接近的词条；低于阈值返回 None。"""
    tags = tags or TAGS_31
    best_tag, best_score = None, 0.0
    for t in tags:
        s = similarity(text, t)
        if s > best_score:
            best_tag, best_score = t, s
    if best_tag and best_score >= threshold:
        return best_tag
    return None


def extract_tags(ocr_texts, tags=None, threshold: float = 0.7) -> list:
    """从一批 OCR 文本里提取去重后的公招词条。

    :param ocr_texts: 形如 ["高级资深干员", "狙击干员", ...] 的文本列表
    """
    found = []
    for text in ocr_texts:
        t = match_tag(text, tags=tags, threshold=threshold)
        if t and t not in found:
            found.append(t)
    return found
