"""日志：控制台 + 文件。"""
import logging
import os
import sys
from datetime import datetime


def setup_logger(log_dir: str = "logs") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)
    name = datetime.now().strftime("recruit_%Y%m%d_%H%M%S.log")
    path = os.path.join(log_dir, name)

    logger = logging.getLogger("ak_recruit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    # 打包成 windowed exe 时 sys.stderr 是 None，加 StreamHandler 会直接报错
    if getattr(sys, "stderr", None) is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info("日志文件: %s", os.path.abspath(path))
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("ak_recruit")
