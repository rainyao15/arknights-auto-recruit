"""公招词条决策引擎。

核心规则（官方拆包 gacha_table.json + MAA recruitment.json 推导）：

1. `recruitRarityTable`：招募时长决定**星级区间**
   - 10min–230min → 1★~4★
   - 240min–450min → 2★~5★
   - 460min–540min → 3★~5★
2. `specialTagRarityTable`：只有两个词条能**改变星级**
   - `高级资深干员` → 6★
   - `资深干员` → 5★
3. 其余 29 个词条**只收窄池子，不提升星级**。也就是说：
   选任何普通词条，最好的情况是"星级不变，可选干员变少"，最坏的情况是
   "把池子收窄到低星"（数据实测：`元素`/`支援机械` 池里最高只有 1★，
   `新手` 池最高 2★）。**划词条风险常数为负收益。**

因此在「不关心抽到谁、只要星级/黄绿票」的目标下，最优策略是：

    有「高级资深干员」→ 只选它               → 6★（唯一能强制高星的词条）
    否则有「资深干员」 → 只选它               → 5★
    否则               → 什么都不选，9h 直接招募 → 3★~5★ 满池随机

最后一步"什么都不选"是本脚本相对"随便选一个"的**关键修正**：
初版规则写的是「任选一个词条」，但当 5 个词条里出现 `新手`/`元素`/
`支援机械` 时，选中它会把收益锁死在 1★~2★，等于白白浪费一个公招位。

fallback 可配置：
  - "none"     默认，不选词条（数学最优）
  - "best_tag" 仍要选一个时，选"不降低星级上限"的词条（兼容老版本行为/强迫症）

lock 模式（给"想锁特定干员收藏"的场景预留，默认关闭）：
  枚举词条组合，在**不降低星级上限**的前提下找命中干员最少（最精确）的组合。
"""
from itertools import combinations
from typing import Optional

SPECIAL_6 = "高级资深干员"
SPECIAL_5 = "资深干员"

# 与 MAA data 解耦：即便 operators 为空也能判断"哪些词条会降星"
DOWNGRADE_TAGS = {"新手", "元素", "支援机械"}
# 这两个词条在 MAA 的 operators[].tags 里没有标注（性别单独存），
# 不能当成"空池"处理，视为不降星。
GENDER_TAGS = {"男性干员", "女性干员"}

# 9 小时招募的星级区间上限（recruitRarityTable: 460min-540min → 3★~5★）。
# 但 5★/6★ 干员**全部**带「资深干员」/「高级资深干员」词条，
# 不选这两个词条时它们根本进不了池子 → 普通词条的实际可达上限是 4★。
HOUR9_MAX_RARITY = 5
PLAIN_MAX_RARITY = 4


def decide(tags, mode: str = "basic", operators: Optional[list] = None,
           fallback: str = "none"):
    """返回 (选中的词条列表, 决策说明)。

    :param tags: 当前公招位刷出的词条（OCR 结果）
    :param mode: "basic" | "lock"
    :param operators: 干员数据（决定星级上限时使用）
    :param fallback: "none"（默认，不选词条）| "best_tag"（选一个不降星的）
    """
    tags = [t for t in tags if t]
    if not tags:
        return [], "空词条 → 不选，直接 9h"

    if SPECIAL_6 in tags:
        return [SPECIAL_6], "6★保底：高级资深干员，单独选即可"

    pool = tags_without(tags, SPECIAL_6)
    if SPECIAL_5 in pool:
        return [SPECIAL_5], "5★保底：资深干员，单独选即可"

    if operators:
        g = guarantee_best(pool, operators)
        if g:
            combo, lo, hi, size = g
            return combo, (f"{lo}★保底：「{'+'.join(combo)}」池内最低 {lo}★、最高 {hi}★"
                           f"（{size} 人），比不选词条的 3★~4★ 随机更值")

    if mode == "lock" and operators:
        combo = lock_best(tags, operators)
        if combo:
            return combo, f"锁定: {describe_lock(combo, operators)}"

    if fallback == "best_tag":
        best = best_safe_tag(tags, operators)
        if best:
            return [best], f"无特殊词条，选不降星词条「{best}」（收益与不选相同）"

    return [], ("无特殊词条 → 不选任何词条，9h 随机 3★~4★"
                "（5★/6★ 必须靠资深/高级资深词条；选普通词条只会降星）")


def guarantee_best(tags, operators, max_size: int = 3, min_gain: int = 4):
    """找「保底星级最高」的词条组合（1~3 个词条）。

    这是「召唤」这类词条真正的价值：
    召唤池里只有 深海色(4★) 等（5★ 的梅尔/稀音 没选资深进不了池子），
    所以 召唤 + 9h = **保底 4★**，收益高于「不选词条，3★~4★ 随机」。

    同理「群攻+减速」「治疗+防护」这类组合也可能把池子压到只剩 4★。

    返回 (combo, 最低星, 最高星, 池大小) 或 None。
    """
    if not operators:
        return None
    best = None
    for r in range(1, min(max_size, len(tags)) + 1):
        for combo in combinations(tags, r):
            pool = match_operators(list(combo), operators)
            if not pool:
                continue
            lo = min(op["rarity"] for op in pool)
            hi = max(op["rarity"] for op in pool)
            if lo < min_gain:
                continue
            key = (lo, hi, -len(pool), -r)      # 保底星 > 上限 > 池子大 > 词条少
            if best is None or key > best[0]:
                best = (key, list(combo), lo, hi, len(pool))
    if not best:
        return None
    return best[1], best[2], best[3], best[4]


def tags_without(tags, *exclude):
    ex = set(exclude)
    return [t for t in tags if t not in ex]


# --------------------------------------------------------------------------
# 星级可达性
# --------------------------------------------------------------------------
def tag_max_rarity(tag: str, operators, cap: int = PLAIN_MAX_RARITY) -> int:
    """单词条在 9h 招募里能给出的**实际**最高星级。

    - `高级资深干员` → 6★，`资深干员` → 5★（specialTagRarityTable）
    - 性别词条：MAA 数据没有标注性别 → 视为不降星（cap）
    - 其他词条：取该词条干员里 ≤cap 的最高星级（cap 默认 4，
      因为 5★/6★ 都带资深词条，没有资深词条时进不了池子）
    """
    if tag == SPECIAL_6:
        return 6
    if tag == SPECIAL_5:
        return 5
    if tag in GENDER_TAGS:
        return cap
    if not operators:
        return {"新手": 2, "元素": 1, "支援机械": 1}.get(tag, cap)
    rs = [op["rarity"] for op in operators if tag in op["tags"] and op["rarity"] <= cap]
    if not rs:
        return 1 if tag in DOWNGRADE_TAGS else cap
    return max(rs)


def combo_max_rarity(combo, operators, cap: int = PLAIN_MAX_RARITY) -> int:
    if not combo:
        return PLAIN_MAX_RARITY
    if SPECIAL_6 in combo:
        return 6
    ms = match_operators(combo, operators)
    if ms:
        return max(op["rarity"] for op in ms)
    return min(tag_max_rarity(t, operators, cap) for t in combo)


def best_safe_tag(tags, operators, cap: int = PLAIN_MAX_RARITY) -> Optional[str]:
    """挑一个"不会把星级上限压低"的词条；优先池子最大的（最不容易被划错）。"""
    best, best_key = None, None
    for t in tags:
        if t in (SPECIAL_6, SPECIAL_5):
            continue
        r = tag_max_rarity(t, operators, cap)
        if r < cap:
            continue
        size = len([op for op in operators if t in op["tags"]]) if operators else 0
        key = (r, size)
        if best_key is None or key > best_key:
            best, best_key = t, key
    return best


def match_operators(combo, operators, max_rarity: Optional[int] = None):
    """返回"真的可能被抽到"的干员。

    除了要求词条全覆盖，还必须满足星级门槛：
    6★ 必须选中「高级资深干员」，5★ 必须选中「资深干员」——
    否则这些干员根本不在池子里（这是初版漏掉的关键约束）。
    """
    cs = set(combo)
    out = []
    for op in operators or []:
        if max_rarity is not None and op["rarity"] > max_rarity:
            continue
        if op["rarity"] == 6 and SPECIAL_6 not in cs:
            continue
        if op["rarity"] == 5 and SPECIAL_5 not in cs:
            continue
        if cs <= set(op["tags"]):
            out.append(op)
    return out


def lock_best(tags, operators, max_rarity: int = 0):
    """枚举词条组合，找「星级上限不被降低」前提下最精确的组合。

    :param max_rarity: 0 = 自动取 9h 区间上限减去特殊词条后的可达星级
    """
    if not operators:
        return None
    cap = max_rarity or PLAIN_MAX_RARITY
    best = None
    for r in range(1, len(tags) + 1):
        for combo in combinations(tags, r):
            best_rarity = combo_max_rarity(combo, operators, cap)
            if best_rarity <= 0:
                continue
            matches = match_operators(combo, operators)
            if not matches:
                continue
            # 星级上限越高越好；同上限下命中干员越少越精确
            key = (best_rarity, -len(matches), -r)
            if best is None or key > best[2]:
                best = (list(combo), best_rarity, key)
    return best[0] if best else None


def describe_lock(combo, operators) -> str:
    matches = match_operators(combo, operators)
    if not matches:
        return "无匹配"
    matches = sorted(matches, key=lambda o: -o["rarity"])
    names = "、".join(f"{op['rarity']}★{op['name']}" for op in matches[:5])
    if len(matches) > 5:
        names += f" 等{len(matches)}人"
    return f"{'+'.join(combo)} → {names}"


def explain_risky_tags(tags, operators) -> list:
    """返回当前词条里"选了会降星"的词条，用于日志告警。"""
    out = []
    for t in tags:
        if t in (SPECIAL_6, SPECIAL_5):
            continue
        if tag_max_rarity(t, operators) < PLAIN_MAX_RARITY:
            out.append(t)
    return out
