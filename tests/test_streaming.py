"""texture.streaming 的 15 个红测试。

初始状态下 8 个函数都是 ``raise NotImplementedError`` 的桩，
因此本文件恰好产生 15 个 failed，且没有 collection/import/syntax error。
测试用内联字典描述纹理::

    {"id": str, "mips": [bytes_size, ...], "priority": float}

``tests/test_streaming.py`` 不允许修改；要修的是 ``texture/streaming.py``。
"""

import pytest

from texture.streaming import (
    StreamError,
    align_bytes,
    evict_lru,
    fallback_level,
    plan_mips,
    priority_score,
    promote,
    resolve_mip,
    stream_batch,
)


# 缺陷 1：只按请求顺序加载，高优先纹理被挤出。
def test_high_priority_texture_wins_under_budget():
    textures = [
        {"id": "low", "mips": [1024], "priority": 0.2},
        {"id": "high", "mips": [1024], "priority": 0.9},
    ]
    result = plan_mips(textures, 1024)
    assert result["resident"] == {"high": 0}
    assert result["bytes"] == 1024


# 缺陷 2：预算按纹理数量而不是字节数，超显存才报错。
def test_budget_counts_bytes_not_texture_count():
    textures = [
        {"id": "a", "mips": [300], "priority": 0.9},
        {"id": "b", "mips": [300], "priority": 0.5},
        {"id": "c", "mips": [300], "priority": 0.1},
    ]
    result = plan_mips(textures, 600)
    assert result["resident"] == {"a": 0, "b": 0}
    assert result["bytes"] == 600


# 缺陷 3：淘汰按加入顺序（FIFO），正在使用的纹理被踢掉。
def test_evict_lru_keeps_textures_in_use():
    resident = {
        "c": {"bytes": 100, "last_used": 1, "in_use": True},
        "a": {"bytes": 100, "last_used": 5, "in_use": False},
        "b": {"bytes": 100, "last_used": 2, "in_use": False},
    }
    result = evict_lru(resident, 200, tick=10)
    assert result["evicted"] == ["b", "a"]
    assert result["freed"] == 200
    assert "c" not in result["evicted"]


# 缺陷 4：升级不做预算检查，先加载再淘汰导致瞬间超限。
def test_promote_refuses_when_budget_is_insufficient():
    texture = {"id": "rock", "mips": [2048, 512, 128], "priority": 0.8}
    refused = promote(texture, 0, {"used": 900, "limit": 1000})
    assert refused == {"ok": False}
    accepted = promote(texture, 2, {"used": 100, "limit": 1000})
    assert accepted == {"ok": True, "bytes": 128, "new_used": 228}


# 缺陷 5：缺失 mip 回退到 0 级，显存翻 4 倍。
def test_fallback_level_never_returns_missing_level():
    texture = {"id": "terrain", "mips": [4096, 1024, 256, 64], "priority": 0.9}
    assert fallback_level(texture, {0}) == 1
    assert texture["mips"][fallback_level(texture, {0})] == 1024
    assert fallback_level(texture, {0, 1}) == 2
    assert fallback_level(texture, {0, 1, 2, 3}) == 3
    assert fallback_level(texture, {1, 3}) == 0
    assert fallback_level(texture, {-5, 99}) == 0


# 缺陷 6：字节对齐向下取整，DDS 行宽错位。
def test_align_bytes_rounds_up_for_dds_row_pitch():
    assert align_bytes(1, 4) == 4
    assert align_bytes(13, 16) == 16
    assert align_bytes(17, 16) == 32
    assert align_bytes(64, 64) == 64
    assert align_bytes(65, 64) == 128
    with pytest.raises(StreamError):
        align_bytes(10, 8)


# 缺陷 7：resolve_mip 不设下限，128 的 8 级算出 0。
def test_resolve_mip_clamps_to_one():
    assert resolve_mip(128, 8) == 1
    assert resolve_mip(128, 20) == 1
    assert resolve_mip(128, 0) == 128
    assert resolve_mip(128, 3) == 16


# 缺陷 8：覆盖率与距离未归一，优先级分数超出 0..1。
def test_priority_score_stays_within_unit_interval():
    texture = {"id": "sky", "mips": [1024, 256], "priority": 1.0}
    assert priority_score(texture, 2.0, -5) == pytest.approx(1.0)
    assert priority_score(texture, -1.0, 100) == pytest.approx(0.0)
    assert 0.0 <= priority_score(texture, 0.5, 10) <= 1.0
    near = priority_score(texture, 0.8, 1)
    far = priority_score(texture, 0.8, 100)
    assert near > far
    low_coverage = priority_score(texture, 0.2, 1)
    high_coverage = priority_score(texture, 0.9, 1)
    assert high_coverage > low_coverage


# 缺陷 9：批量流送遇损坏纹理只跳过，输出里留下半加载状态。
def test_stream_batch_rolls_back_on_corrupt_texture():
    requests = [
        {"texture": {"id": "good", "mips": [256], "priority": 1.0}, "level": 0},
        {"texture": {"id": "bad", "mips": [], "priority": 1.0}, "level": 0},
    ]
    result = stream_batch(requests, 4096)
    assert result == {"ok": False, "resident": {}, "bytes": 0, "skipped": []}


# 缺陷 10：同一纹理重复请求不去重，出现两份驻留记录。
def test_stream_batch_deduplicates_same_texture():
    texture = {"id": "wall", "mips": [512, 128], "priority": 0.7}
    requests = [
        {"texture": texture, "level": 0},
        {"texture": texture, "level": 0},
    ]
    result = stream_batch(requests, 4096)
    assert result["ok"] is True
    assert result["resident"] == {"wall": 0}
    assert result["bytes"] == 512


# 组合：空请求返回空。
def test_empty_requests_yield_empty_plan():
    assert plan_mips([], 0) == {"resident": {}, "bytes": 0}


# 组合：budget_bytes=0 时只允许 0 字节。
def test_zero_budget_allows_zero_bytes():
    texture = {"id": "huge", "mips": [1024, 256], "priority": 1.0}
    assert plan_mips([texture], 0) == {"resident": {}, "bytes": 0}


# 组合：resolve_mip(128, 7) == 1。
def test_resolve_mip_128_at_level_7_is_one():
    assert resolve_mip(128, 7) == 1


# 组合：align_bytes 幂等。
def test_align_bytes_is_idempotent():
    for size in (0, 1, 15, 16, 17, 63, 64, 65, 127):
        once = align_bytes(size, 16)
        assert align_bytes(once, 16) == once


# 组合：批量全成功时驻留字节数等于各纹理之和。
def test_stream_batch_all_success_resident_bytes_sum():
    requests = [
        {"texture": {"id": "a", "mips": [256, 64], "priority": 0.5}, "level": 0},
        {"texture": {"id": "b", "mips": [128, 32], "priority": 0.9}, "level": 1},
        {"texture": {"id": "c", "mips": [64], "priority": 0.1}, "level": 0},
    ]
    budget = 256 + 32 + 64
    result = stream_batch(requests, budget)
    assert result["ok"] is True
    assert result["resident"] == {"a": 0, "b": 1, "c": 0}
    assert result["bytes"] == 352
    assert result["skipped"] == []
