"""配置加载。"""
import os

import yaml


class Config:
    """薄封装：让 cfg["adb"]["ports"] 也可用 cfg.adb.ports 访问。"""

    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def __getattr__(self, name):
        try:
            v = self._data[name]
        except KeyError:
            raise AttributeError(name)
        if isinstance(v, dict):
            return Config(v)
        return v


def load_config(path: str) -> Config:
    path = os.path.abspath(path)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # 把相对路径都解析成基于配置文件所在目录的绝对路径
    base = os.path.dirname(path)
    if "data" in data and "cache_path" in data["data"]:
        cp = data["data"]["cache_path"]
        if not os.path.isabs(cp):
            data["data"]["cache_path"] = os.path.join(base, cp)
    return Config(data)
