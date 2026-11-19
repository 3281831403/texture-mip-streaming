"""贴图流送核心模块（题目桩：8 个函数全部未实现）。

统一约定
--------
- 纹理用内联字典描述::

      {"id": str, "mips": [bytes_size, ...], "priority": float}

  ``mips[0]`` 是最精细的 0 级 mip（全分辨率），下标越大越粗糙、
  字节数越少。所有 mip 字节数都是正整数。
- ``priority`` 越大表示越重要，取值约定在 0.0..1.0。
- 所有函数只做确定性的纯计算：不读文件、不联网、不使用随机数。
- 非法输入（负预算、越界级别等）抛 :class:`StreamError`。
"""

import heapq
import math
from collections import OrderedDict

__all__ = [
    "StreamError",
    "plan_mips",
    "priority_score",
    "evict_lru",
    "promote",
    "fallback_level",
    "align_bytes",
    "resolve_mip",
    "stream_batch",
]


class StreamError(Exception):
    """贴图流送过程中的非法输入或状态错误。"""


def plan_mips(textures, budget_bytes):
    """在字节预算内为每张纹理规划驻留的 mip 级别。

    参数:
        textures: 可迭代的纹理字典，字段见模块说明。
        budget_bytes: 非负整数；所有驻留纹理的 ``mips[level]`` 之和
            不得超过该预算。

    规划规则:
        - 按 ``priority`` 从大到小处理；``priority`` 相同按 ``id``
          升序处理，保证结果与输入顺序无关且确定。
        - 对每张纹理，从 0 级开始逐级增大，选择第一个满足
          ``mips[level] <= 剩余预算`` 的级别，即能放下的最精细级别。
        - 若连最粗糙（最后一层）级别都放不下，该纹理不驻留，
          不计入字节数。
        - 同一 ``id`` 只出现一次。

    返回:
        ``{"resident": {id: level}, "bytes": 总驻留字节数}``；
        ``bytes`` 一定满足 ``0 <= bytes <= budget_bytes``。
    """
    if budget_bytes < 0:
        raise StreamError("budget_bytes 必须是非负整数")

    # 同一 id 去重，只保留第一次出现的纹理记录。
    unique = {}
    for texture in textures:
        tid = texture["id"]
        if tid not in unique:
            unique[tid] = texture

    # 优先级降序、id 升序：按重要性而不是请求顺序分配预算。
    ordered = sorted(
        unique.values(),
        key=lambda texture: (-texture["priority"], texture["id"]),
    )

    resident = {}
    used_bytes = 0
    for texture in ordered:
        mips = texture["mips"]
        chosen_level = None
        for level, size in enumerate(mips):
            # 按字节核算：第一个放得下的级别就是最精细的可用级别。
            if used_bytes + size <= budget_bytes:
                chosen_level = level
                break
        if chosen_level is None:
            # 连最粗糙的一级都放不下，该纹理不驻留。
            continue
        resident[texture["id"]] = chosen_level
        used_bytes += mips[chosen_level]

    return {"resident": resident, "bytes": used_bytes}


def priority_score(texture, screen_coverage, distance):
    """把覆盖率、距离和纹理优先级映射成 0..1 的流送分数。

    参数:
        texture: 纹理字典，使用其中的 ``priority``。
        screen_coverage: 屏幕覆盖率，先夹取到 0.0..1.0。
        distance: 到相机的距离，负数按 0 处理。

    计算规则（结果确定、无随机）::

        coverage = min(max(screen_coverage, 0.0), 1.0)
        dist     = max(float(distance), 0.0)
        priority = min(max(texture["priority"], 0.0), 1.0)
        score    = priority * coverage / (1.0 + dist)
        return min(max(score, 0.0), 1.0)

    单调性：对 ``screen_coverage`` 单调不减，对 ``distance`` 单调不增；
    返回值恒在 0.0..1.0 内，不会越界。浮点比较使用 1e-6 容差
    （测试中对应 ``pytest.approx`` 的默认容差）。
    """
    coverage = min(max(screen_coverage, 0.0), 1.0)
    dist = max(float(distance), 0.0)
    priority = min(max(texture["priority"], 0.0), 1.0)
    score = priority * coverage / (1.0 + dist)
    return min(max(score, 0.0), 1.0)


def evict_lru(resident, need_bytes, tick):
    """按最近使用时间（LRU）淘汰纹理，返回淘汰结果。

    参数:
        resident: ``{id: {"bytes": int, "last_used": int, "in_use": bool}}``。
            ``last_used`` 是最后一次使用的 tick，``in_use`` 为 True
            表示正在使用。
        need_bytes: 需要腾出的字节数，非负整数。
        tick: 当前帧号；``last_used >= tick`` 的条目同样视为正在使用。

    淘汰规则:
        - 正在使用的条目（``in_use`` 为 True，或
          ``last_used >= tick``）绝不淘汰。
        - 其余条目按 ``last_used`` 升序淘汰（最久未使用优先）；
          ``last_used`` 相同按 ``id`` 升序，保证确定性。
        - 依次淘汰，直到 ``freed >= need_bytes``；可淘汰总量不足时，
          淘汰全部可淘汰条目后返回。
        - ``need_bytes <= 0`` 时不淘汰任何条目。

    返回:
        ``{"evicted": [id, ...], "freed": int}``，``evicted`` 按实际
        淘汰顺序排列，``freed`` 是被淘汰条目的字节数之和。
    """
    if need_bytes <= 0:
        return {"evicted": [], "freed": 0}

    # 保护正在使用的纹理，其余才是淘汰候选。
    candidates = [
        (tid, info)
        for tid, info in resident.items()
        if not info["in_use"] and info["last_used"] < tick
    ]
    # LRU：最久未使用优先；同 tick 时按 id 升序保证确定性。
    candidates.sort(key=lambda item: (item[1]["last_used"], item[0]))

    evicted = []
    freed = 0
    for tid, info in candidates:
        if freed >= need_bytes:
            break
        evicted.append(tid)
        freed += info["bytes"]

    return {"evicted": evicted, "freed": freed}


def promote(texture, level, budget):
    """把纹理升级（细化）到指定 mip 级别，升级前必须先确认预算。

    参数:
        texture: 纹理字典。
        level: 目标 mip 级别，必须在 ``[0, len(mips) - 1]`` 内。
        budget: ``{"used": int, "limit": int}``，分别是已用字节和硬上限。

    规则:
        - 非法级别抛 :class:`StreamError`。
        - 升级需要额外加载 ``mips[level]`` 字节；只有当
          ``used + mips[level] <= limit`` 时才允许。
        - 预算不足时直接返回 ``{"ok": False}``；不得先加载再淘汰，
          也不得让占用瞬间超过 ``limit``。
        - 预算足够时返回
          ``{"ok": True, "bytes": mips[level],
          "new_used": used + mips[level]}``。
    """
    mips = texture["mips"]
    if not isinstance(level, int) or level < 0 or level >= len(mips):
        raise StreamError("非法 mip 级别: %r" % (level,))

    used = budget["used"]
    limit = budget["limit"]
    size = mips[level]

    # 先校验预算再装载，杜绝瞬间超限。
    if used + size <= limit:
        return {"ok": True, "bytes": size, "new_used": used + size}
    return {"ok": False}


def fallback_level(texture, missing_mips):
    """在部分 mip 缺失时挑选可用的回退级别，绝不越界。

    参数:
        texture: 纹理字典。
        missing_mips: 不可用的 mip 级别集合（可迭代）；越界值忽略。

    规则:
        - 从 0 级开始逐级增大，返回第一个不在 ``missing_mips`` 中的
          级别（可用的最精细级别）。
        - 若所有级别都缺失，返回最后一层 ``len(mips) - 1``
          （最粗糙的一层）。
        - 返回值一定满足 ``0 <= level <= len(mips) - 1``；
          绝不返回缺失级别，也绝不无条件回退到 0 级。
        - ``mips`` 为空时抛 :class:`StreamError`。
    """
    mips = texture["mips"]
    if not mips:
        raise StreamError("纹理没有任何 mip 级别")

    missing = set(missing_mips)
    for level in range(len(mips)):
        if level not in missing:
            return level
    # 全部缺失时回退到最粗糙的一层，而不是无条件回到 0 级。
    return len(mips) - 1


def align_bytes(size, alignment):
    """把字节数向上对齐到 ``alignment`` 的整数倍。

    参数:
        size: 原始字节数，非负整数。
        alignment: 只允许 4、16、64（DDS 行宽对齐的常见取值）。

    规则:
        - 返回最小的 ``n >= size`` 且 ``n % alignment == 0``。
        - ``size == 0`` 返回 0；对齐必须向上取整，不能向下取整。
        - 非法 ``alignment`` 或负数 ``size`` 抛 :class:`StreamError`。
        - 幂等：``align_bytes(align_bytes(x, a), a) == align_bytes(x, a)``。
    """
    if size < 0:
        raise StreamError("size 必须是非负整数")
    if alignment not in (4, 16, 64):
        raise StreamError("alignment 只允许 4、16、64，收到 %r" % (alignment,))
    if size == 0:
        return 0
    # 向上取整：((size + a - 1) // a) * a，保证 DDS 行宽不错位。
    return ((size + alignment - 1) // alignment) * alignment


def resolve_mip(size, level):
    """按 ``2 ** level`` 缩放 mip 尺寸，结果最小为 1。

    参数:
        size: 0 级尺寸，正整数。
        level: 非负整数 mip 级别。

    规则:
        - 返回 ``max(1, size // (2 ** level))``，即每升一级尺寸减半，
          且不设下限时可能出现的 0 必须夹到 1。
        - ``size`` 非正或 ``level`` 为负时抛 :class:`StreamError`。
    """
    if size <= 0:
        raise StreamError("size 必须是正整数")
    if level < 0:
        raise StreamError("level 必须是非负整数")
    return max(1, size // (2 ** level))


def stream_batch(requests, budget):
    """批量流送纹理请求：按优先级装载、超预算跳过、损坏则整体回滚。

    参数:
        requests: ``[{"texture": 纹理字典, "level": int}, ...]``。
        budget: 非负字节预算。

    校验（先校验全部请求，再装载任何纹理，保证原子性）:
        - ``mips`` 为空、任一 mip 字节数 ``<= 0``、或 ``level`` 不在
          ``[0, len(mips) - 1]`` 内，均视为损坏请求。
        - 只要有一条损坏，立刻返回
          ``{"ok": False, "resident": {}, "bytes": 0, "skipped": []}``：
          不装载任何纹理，不留下半加载状态（整体回滚）。
        - 重复 ``id`` 的请求合并成一条，只保留最精细（最小）的
          ``level``；字节数只计一次。

    装载规则:
        - 去重后按 ``priority`` 降序、``id`` 升序处理。
        - 若 ``bytes + mips[level] <= budget`` 则驻留；否则把该 ``id``
          放进 ``skipped``，继续处理后面的请求。
        - 返回 ``{"ok": True, "resident": {id: level},
          "bytes": 总驻留字节数, "skipped": [id, ...]}``；
          ``bytes`` 一定不超过 ``budget``。
    """
    if budget < 0:
        raise StreamError("budget 必须是非负整数")

    rollback = {"ok": False, "resident": {}, "bytes": 0, "skipped": []}

    # 第一阶段：校验全部请求。任何一条损坏都整体回滚，不装载任何纹理。
    for request in requests:
        texture = request["texture"]
        mips = texture.get("mips", [])
        level = request["level"]
        if (
            not mips
            or any(size <= 0 for size in mips)
            or not isinstance(level, int)
            or level < 0
            or level >= len(mips)
        ):
            return rollback

    # 第二阶段：按 id 去重，同一纹理只保留最精细（最小）的级别。
    deduped = {}
    for request in requests:
        texture = request["texture"]
        tid = texture["id"]
        level = request["level"]
        if tid not in deduped or level < deduped[tid][1]:
            deduped[tid] = (texture, level)

    # 按优先级降序、id 升序装载，与输入请求顺序无关。
    ordered = sorted(
        deduped.values(),
        key=lambda item: (-item[0]["priority"], item[0]["id"]),
    )

    resident = {}
    used_bytes = 0
    skipped = []
    for texture, level in ordered:
        tid = texture["id"]
        size = texture["mips"][level]
        # 按字节核算预算：放不下则跳过该纹理，继续处理后续请求。
        if used_bytes + size <= budget:
            resident[tid] = level
            used_bytes += size
        else:
            skipped.append(tid)

    return {
        "ok": True,
        "resident": resident,
        "bytes": used_bytes,
        "skipped": skipped,
    }
