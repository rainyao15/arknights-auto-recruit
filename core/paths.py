"""路径解析：源码运行 / PyInstaller 打包成 exe 后都能找到 config、data、logs。

规则：
  - 源码运行  → 以项目根目录（本文件的上一级）为根；
  - 打包运行  → 以 exe 所在目录为根（config.yaml / data / logs 都放在 exe 旁边，
                用户随时能改、能看，不会被塞进只读的 _internal 里）。
"""
import os
import shutil
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


def _bundle(*parts) -> str:
    """打包后 PyInstaller 的解压目录（源码运行时返回空串）。"""
    base = getattr(sys, "_MEIPASS", None)
    return os.path.join(base, *parts) if base else ""


def ensure_user_files():
    """冻结版首次启动：把内置的 config.yaml / 词条数据放到 exe 旁边。

    这样 exe 不用带着一堆文件发出去 —— 用户第一次双击就自动长出一份可编辑的
    config.yaml 和 data/recruitment.json，之后改配置直接改 exe 旁边那份。
    """
    if not FROZEN:
        return
    pairs = (
        (_bundle("_defaults", "config.yaml"), CONFIG_PATH),
        (_bundle("_defaults", "data", "recruitment.json"),
         os.path.join(DATA_DIR, "recruitment.json")),
    )
    for src, dst in pairs:
        if not src or not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        except OSError:
            pass


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
