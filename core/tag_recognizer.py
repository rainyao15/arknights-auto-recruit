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


# 两字词条是识别最脆弱的一类：OCR 错一个字，相似度就掉到 0.5，
# 直接低于 0.7 阈值被丢掉（实测：'削羽'→None、'输岀'→None、'群玫'→None、
# '近站位'→0.67→None）。丢一条就可能把「削弱」这种 4★ 保底词条漏掉。
SHORT_TAG = 2


def match_tag(text: str, tags=None, threshold: float = 0.7) -> str | None:
    """把一段 OCR 文本匹配到最接近的词条；低于阈值返回 None。

    两字词条额外给一条「唯一最优」容错：
      * 相似度只要求 ≥0.45，
      * 但必须比第二名高出 ≥0.20（避免「输出/生存」这种等距歧义瞎猜）。
    三字以上词条保持原阈值（它们错一个字仍有 0.75，不需要放宽）。
    """
    tags = tags or TAGS_31
    scored = sorted(((similarity(text, t), t) for t in tags), reverse=True)
    if not scored:
        return None
    best_score, best_tag = scored[0]
    if best_score >= threshold:
        return best_tag
    if len(_norm(best_tag)) == SHORT_TAG:
        second = scored[1][0] if len(scored) > 1 else 0.0
        if best_score >= 0.45 and (best_score - second) >= 0.20:
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
