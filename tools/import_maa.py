"""把 MAA 的公开招募词条库同步进本程序。

数据源：MaaAssistantArknights/resource/recruitment.json
        {"tags": {"削弱": "削弱", ...}, "operators": [{"id","name","rarity","tags"}]}

本程序内部用的就是这份 schema（见 core/data_loader.py），所以这里是「拉取 → 校验 →
合并 → 备份 → 落盘 → 同步到 exe 旁边那份 data/」。

用法：
    python tools/import_maa.py              # 拉最新，校验后写入（有差异才写）
    python tools/import_maa.py --check      # 只体检本地数据，不联网
    python tools/import_maa.py --dry-run    # 联网比对，但不写文件
    python tools/import_maa.py --url URL    # 指定镜像/分支
    python tools/import_maa.py --force      # 即使没差异也重写（刷新文件时间）

设计原则：
  * 只做「公招池」的数据。MAA 这份就是公招可获得干员 + 31 个词条，不要拿全干员表
    去合并 —— 那会把不在公招池里的干员算进保底池，决策会给出根本不存在的保底。
  * 本地补充（MAA 还没有的新干员）不会被覆盖：按 id 合并，远端优先同 id，本地多出的保留。
  * 任何写入前先备份到 data/backup/。
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_URLS = [
    "https://cdn.jsdelivr.net/gh/MaaAssistantArknights/MaaAssistantArknights@master/resource/recruitment.json",
    "https://raw.githubusercontent.com/MaaAssistantArknights/MaaAssistantArknights/master/resource/recruitment.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/MaaAssistantArknights/MaaAssistantArknights/master/resource/recruitment.json",
]

# 31 个公招词条（与 core/data_loader.TAGS_31 同源，用于交叉校验）
TAGS_31 = [
    "高级资深干员", "资深干员",
    "近卫干员", "狙击干员", "重装干员", "医疗干员", "辅助干员", "术师干员", "特种干员", "先锋干员",
    "近战位", "远程位", "男性干员", "女性干员",
    "新手", "治疗", "支援", "输出", "生存", "群攻", "防护", "减速", "削弱", "控场", "爆发",
    "快速复活", "位移", "召唤", "支援机械", "费用回复", "元素",
]


# --------------------------------------------------------------------- 读取
def load_local(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch(url: str = None, timeout: int = 30) -> dict:
    import requests
    urls = [url] if url else DEFAULT_URLS
    last = None
    for u in urls:
        try:
            r = requests.get(u, timeout=timeout, headers={"User-Agent": "ak-recruit-bot/import"})
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict) or "operators" not in data:
                raise ValueError("结构不像 MAA recruitment.json")
            print(f"  数据源: {u}")
            return data
        except Exception as e:                      # 换下一个镜像
            print(f"  [WARN] {u}\n         {e}")
            last = e
    raise RuntimeError(f"所有镜像都失败: {last}")


# --------------------------------------------------------------------- 校验
def validate(data: dict) -> tuple:
    """返回 (errors, warnings, stats)。errors 非空就别写盘。"""
    errors, warnings = [], []
    tags = data.get("tags") or {}
    ops = data.get("operators") or []

    if not isinstance(tags, dict):
        errors.append("tags 应该是 dict（词条名->词条名）")
        tags = {}
    if not isinstance(ops, list) or not ops:
        errors.append("operators 为空或不是 list")

    miss = [t for t in TAGS_31 if t not in tags]
    extra = [t for t in tags if t not in TAGS_31]
    if miss:
        warnings.append(f"缺少 {len(miss)} 个标准词条: {'、'.join(miss)}")
    if extra:
        warnings.append(f"多出未知词条（新版本新增？）: {'、'.join(extra)}")

    seen_id, seen_name = {}, {}
    rar = {}
    for op in ops:
        oid = (op.get("id") or "").strip()
        name = (op.get("name") or "").strip()
        rarity = op.get("rarity")
        optags = op.get("tags") or []
        if not oid or not name:
            errors.append(f"缺 id/name 的条目: {op}")
            continue
        if not isinstance(rarity, int) or not 1 <= rarity <= 6:
            errors.append(f"{name} 星级异常: {rarity!r}")
        if not optags:
            warnings.append(f"{name} 没有任何词条")
        bad = [t for t in optags if t not in tags and t not in TAGS_31]
        if bad:
            warnings.append(f"{name} 的词条不在白名单: {'、'.join(bad)}")
        if oid in seen_id:
            errors.append(f"id 重复: {oid}（{seen_id[oid]} / {name}）")
        if name in seen_name:
            warnings.append(f"重名干员: {name}")
        seen_id[oid] = name
        seen_name[name] = oid
        rar[rarity] = rar.get(rarity, 0) + 1

    # 公招常识校验：高级资深必须只对应 6★，资深只对应 5★
    for t, want in (("高级资深干员", 6), ("资深干员", 5)):
        got = {op.get("rarity") for op in ops if t in (op.get("tags") or [])}
        got.discard(None)
        if got - {want}:
            warnings.append(f"「{t}」池里出现了非 {want}★: {sorted(got)}")

    stats = {
        "operators": len(ops),
        "tags": len(tags),
        "by_rarity": dict(sorted(rar.items())),
    }
    return errors, warnings, stats


# --------------------------------------------------------------------- 合并/差异
def merge(remote: dict, local: dict) -> tuple:
    """按 id 合并：远端优先，本地多出的保留。返回 (merged, diff)。"""
    r_ops = {o["id"]: o for o in remote.get("operators") or []}
    l_ops = {o["id"]: o for o in local.get("operators") or []}
    out = dict(l_ops)
    out.update(r_ops)                       # 同 id 以远端为准
    merged = {
        "tags": dict(local.get("tags") or {}, **{k: v for k, v in (remote.get("tags") or {}).items()}),
        "operators": [out[k] for k in sorted(out)],
    }
    added = [r_ops[k]["name"] for k in r_ops if k not in l_ops]
    removed = [l_ops[k]["name"] for k in l_ops if k not in r_ops]
    changed = []
    for k in r_ops:
        if k in l_ops:
            a, b = l_ops[k], r_ops[k]
            if (a.get("rarity"), sorted(a.get("tags") or [])) != (b.get("rarity"), sorted(b.get("tags") or [])):
                changed.append(b.get("name") or k)
    diff = {"added": added, "removed": removed, "changed": changed,
            "tag_added": [t for t in (remote.get("tags") or {}) if t not in (local.get("tags") or {})]}
    return merged, diff


# --------------------------------------------------------------------- 落盘
def targets() -> list:
    """要写的路径：源码那份 + 已存在的 exe 旁那份。"""
    out = [os.path.join(ROOT, "data", "recruitment.json")]
    for d in ("dist/明日方舟公招面板/data/recruitment.json",
              "dist_new/明日方舟公招面板/data/recruitment.json"):
        p = os.path.join(ROOT, d)
        if os.path.exists(p):
            out.append(p)
    return out


def backup(path: str) -> str:
    if not os.path.exists(path):
        return ""
    bdir = os.path.join(ROOT, "data", "backup")
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, f"recruitment-{datetime.now():%Y%m%d-%H%M%S}.json")
    shutil.copy2(path, dst)
    return dst


def write_all(data: dict) -> list:
    written = []
    for p in targets():
        os.makedirs(os.path.dirname(p), exist_ok=True)
        bak = backup(p)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        written.append((p, bak))
    return written


# --------------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="同步 MAA 公招词条库")
    ap.add_argument("--url", default=None, help="指定数据源（默认走镜像列表）")
    ap.add_argument("--check", action="store_true", help="只体检本地数据，不联网")
    ap.add_argument("--dry-run", action="store_true", help="联网比对但不写文件")
    ap.add_argument("--force", action="store_true", help="无差异也重写")
    args = ap.parse_args()

    local_path = os.path.join(ROOT, "data", "recruitment.json")
    local = load_local(local_path) if os.path.exists(local_path) else {"tags": {}, "operators": []}
    errs, warns, stats = validate(local)
    print("本地数据:", json.dumps(stats, ensure_ascii=False))
    for w in warns:
        print("  [WARN]", w)
    for e in errs:
        print("  [ERR ]", e)
    if args.check:
        return 1 if errs else 0

    print("\n拉取 MAA 最新数据…")
    remote = fetch(args.url)
    rerrs, rwarns, rstats = validate(remote)
    print("远端数据:", json.dumps(rstats, ensure_ascii=False))
    for w in rwarns:
        print("  [WARN]", w)
    if rerrs:
        for e in rerrs:
            print("  [ERR ]", e)
        print("远端数据不合格 → 不写入")
        return 1

    merged, diff = merge(remote, local)
    there_is_diff = any(diff[k] for k in ("added", "removed", "changed", "tag_added"))
    print("\n差异: 新增 %d / 移除 %d / 变更 %d / 新词条 %d"
          % (len(diff["added"]), len(diff["removed"]), len(diff["changed"]), len(diff["tag_added"])))
    for k, title in (("added", "新增干员"), ("removed", "移除干员"),
                     ("changed", "星级/词条变化"), ("tag_added", "新增词条")):
        if diff[k]:
            print(f"  {title}: {'、'.join(diff[k])}")

    if not there_is_diff and not args.force:
        print("\n已是最新，不写文件。")
        return 0
    if args.dry_run:
        print("\n--dry-run：不写文件。")
        return 0

    merged["generated_at"] = datetime.now().isoformat(timespec="seconds")
    merged["source"] = args.url or DEFAULT_URLS[0]
    for p, bak in write_all(merged):
        print(f"已写入: {p}" + (f"（备份: {bak}）" if bak else ""))
    print("\n完成。重开面板即生效（面板启动时读 data/recruitment.json）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
