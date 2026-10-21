# -*- coding: utf-8 -*-
"""场次解析缓存（_FRAMES_CACHE）的计数与驱逐测试。

背景：缓存用「条目数 + 总帧数」双上限做 LRU，`_CACHED_FRAMES_TOTAL` 是一个
手工维护的计数器，只有在 `_frames_cache_forget()` 里才会减少。

正在录制的场次每次刷新都会变 mtime/size → 同一路径被反复重新解析。
如果覆盖字典条目时不先 forget，计数只增不减：虚高到超过上限后，
`_frames_cache_evict()` 会在**每次冷解析前**把缓存清空，缓存退化成
「只能存 1 场」，用户在场次之间切换永远冷解析（一场 20 万帧约 2~3 秒）。
"""
import json

import pytest


def _write_session(path, n: int, t0: float = 0.0) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "x", "started_at": 1.0}) + "\n")
        for i in range(n):
            f.write(json.dumps({
                "t": round(t0 + i * 0.02, 4), "lap": 1,
                "speed_kph": 100.0, "rpm": 5000.0, "gear": 3,
                "throttle": 0.5, "brake": 0.0, "car_x": 1.0, "car_z": 2.0,
                "g_force": [0.0, 0.0, 0.0],
            }, separators=(",", ":")) + "\n")


def _reset(dash) -> None:
    with dash._CACHE_LOCK:
        dash._FRAMES_CACHE.clear()
        dash._CACHED_FRAMES_TOTAL = 0


def _cached_frames(dash) -> int:
    """缓存里**实际**存了多少帧（与计数器对比的基准）。"""
    return sum(len(e[3]) for e in dash._FRAMES_CACHE.values())


def test_frames_cache_counter_conserved(dash, tmp_path):
    """重复解析同一路径后，计数器必须等于缓存里的真实帧数。"""
    _reset(dash)
    p = tmp_path / "20261021_120000_Test.jsonl"
    for n in (10, 20, 30, 40):
        _write_session(p, n, t0=n)      # 内容变化 → mtime/size 变化 → 缓存失效
        dash._load_frames(p)
        assert dash._CACHED_FRAMES_TOTAL == _cached_frames(dash), (
            f"解析 {n} 帧后计数器虚高："
            f"total={dash._CACHED_FRAMES_TOTAL}，实际={_cached_frames(dash)}")
    # 最后一次解析了 40 帧，缓存里也只应该有 40 帧
    assert dash._CACHED_FRAMES_TOTAL == 40


def test_frames_cache_evicts_to_cap(dash, tmp_path):
    """总帧数上限必须真正生效：超了就驱逐最久未用的条目。"""
    _reset(dash)
    old_cap = dash._FRAME_CACHE_MAX_FRAMES
    try:
        dash._FRAME_CACHE_MAX_FRAMES = 25
        a = tmp_path / "a.jsonl"
        b = tmp_path / "b.jsonl"
        _write_session(a, 20, t0=0.0)
        _write_session(b, 20, t0=100.0)
        dash._load_frames(a)
        dash._load_frames(b)
        assert dash._CACHED_FRAMES_TOTAL <= 25, (
            f"超过上限未驱逐：total={dash._CACHED_FRAMES_TOTAL}")
        assert dash._CACHED_FRAMES_TOTAL == _cached_frames(dash)
        # 只剩最新解析的那一场
        assert len(dash._FRAMES_CACHE) == 1
    finally:
        dash._FRAME_CACHE_MAX_FRAMES = old_cap
        _reset(dash)
