"""路径解析：源码运行 / PyInstaller 打包成 exe 后都能找到 config、data、logs。

规则：
  - 源码运行  → 以项目根目录（本文件的上一级）为根；
  - 打包运行  → 以 exe 所在目录为根（config.yaml / data / logs 都放在 exe 旁边，
                用户随时能改、能看，不会被塞进只读的 _internal 里）。
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))


def app_dir() -> str:
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


ROOT = app_dir()
LOG_DIR = os.path.join(ROOT, "logs")
DATA_DIR = os.path.join(ROOT, "data")
CONFIG_PATH = os.path.join(ROOT, "config.yaml")
HISTORY_PATH = os.path.join(LOG_DIR, "history.jsonl")
HISTORY_SIM_PATH = os.path.join(LOG_DIR, "history_sim.jsonl")


def ensure_dirs():
    for d in (LOG_DIR, DATA_DIR):
        os.makedirs(d, exist_ok=True)
    return ROOT


def rel(*parts) -> str:
    """项目根下的绝对路径。"""
    return os.path.join(ROOT, *parts)


def resolve_config(name: str = None) -> str:
    """优先用 exe 同级的 config.yaml；没有就退回相对路径。"""
    if not name:
        return CONFIG_PATH
    if os.path.isabs(name) and os.path.exists(name):
        return name
    p = os.path.join(ROOT, name)
    return p if os.path.exists(p) else name
