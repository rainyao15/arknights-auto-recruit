"""OCR 封装（rapidocr v3，onnxruntime 后端，离线）。"""
import logging
from dataclasses import dataclass

# 抑制 rapidocr 的 INFO 日志噪音
logging.getLogger("rapidocr").setLevel(logging.WARNING)
logging.getLogger("RapidOCR").setLevel(logging.WARNING)


@dataclass
class OcrText:
    text: str
    box: list      # 四点 [[x,y],...]
    score: float

    @property
    def center(self):
        if not self.box:
            return (0.0, 0.0)
        xs = [p[0] for p in self.box]
        ys = [p[1] for p in self.box]
        return (sum(xs) / len(xs), sum(ys) / len(ys))


class OcrEngine:
    def __init__(self, cfg):
        self.cfg = cfg
        self.min_conf = cfg["ocr"]["min_confidence"]
        self._engine = None

    def _get(self):
        if self._engine is None:
            from rapidocr import RapidOCR
            self._engine = RapidOCR()
        return self._engine

    def recognize(self, img) -> list:
        """img 为 numpy BGR 数组，返回 OcrText 列表。"""
        if img is None:
            return []
        r = self._get()(img)
        if r is None or getattr(r, "txts", None) is None:
            return []
        boxes = r.boxes if getattr(r, "boxes", None) is not None else []
        scores = r.scores if getattr(r, "scores", None) is not None else []
        out = []
        for i, text in enumerate(r.txts):
            score = float(scores[i]) if i < len(scores) else 1.0
            if score < self.min_conf:
                continue
            box = []
            if i < len(boxes):
                box = [[float(p[0]), float(p[1])] for p in boxes[i]]
            out.append(OcrText(str(text), box, score))
        return out

    def texts(self, img) -> list:
        return [t.text for t in self.recognize(img)]

    def find_text(self, img, keyword: str):
        """找包含 keyword 的文本，返回 OcrText 或 None。"""
        for t in self.recognize(img):
            if keyword in t.text or t.text in keyword:
                return t
        return None

    def find_any(self, img, keywords: list):
        for kw in keywords:
            t = self.find_text(img, kw)
            if t:
                return t
        return None

class FastOcr:
    """只做「识别」不做「检测」的极速 OCR（固定位置裁小块喂进去）。

    实测（AMD Vega8 / 1600x900）：
        RapidOCR 全屏           ≈ 4000~6000 ms
        RapidOCR 裁区域(带检测)  ≈ 1000~1500 ms
        FastOcr 单行裁块         ≈   50~70 ms     ← 快 80 倍

    原理：固定位置的小块里只有一行字，检测阶段完全没必要，直接 rec。
    代价：位置得靠 config.ui.fast 标定；分辨率变了要重标（UI.fast_ready() 会把关）。
    """

    def __init__(self):
        self._engine = None

    def _get(self):
        if self._engine is None:
            from rapidocr import RapidOCR
            self._engine = RapidOCR(params={"Global.use_det": False,
                                            "Global.use_cls": False})
        return self._engine

    def line(self, img) -> str:
        """认一块单行小图，返回文字（失败返回空串）。"""
        if img is None or getattr(img, "size", 0) == 0:
            return ""
        try:
            r = self._get()(img)
        except Exception:
            return ""
        txts = getattr(r, "txts", None)
        if not txts:
            return ""
        return "".join(txts)

