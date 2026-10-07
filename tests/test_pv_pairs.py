"""关键点配对（match_pv_pairs / analyze_compare.pv_pairs）测试。

配对用「圈内相对位置」（圈长分数）而不是绝对距离——
两圈速度积分漂移可达上百米，绝对距离配对会把同一弯道配丢。
"""

import pytest

from gt7analysis import analyze_compare, clean_laps, match_pv_pairs


# ---------- 纯函数：match_pv_pairs ----------

def test_basic_pairing_and_delta():
    ref = [{"kind": "valley", "distance": 100.0, "speed_kph": 50.0},
           {"kind": "peak", "distance": 500.0, "speed_kph": 200.0}]
    cur = [{"kind": "valley", "distance": 105.0, "speed_kph": 55.0},
           {"kind": "peak", "distance": 505.0, "speed_kph": 190.0}]
    pairs = match_pv_pairs(ref, cur, 1000.0, 1000.0)
    assert len(pairs) == 2
    assert pairs[0]["kind"] == "valley"
    assert pairs[0]["delta"] == pytest.approx(5.0)
    assert pairs[1]["delta"] == pytest.approx(-10.0)
    # 按距离升序
    assert pairs[0]["distance"] < pairs[1]["distance"]


def test_cross_kind_never_paired():
    """峰不能配谷：直道尾速和弯心速度差着两三百度，跨类配对是荒谬值。"""
    ref = [{"kind": "peak", "distance": 500.0, "speed_kph": 200.0}]
    cur = [{"kind": "valley", "distance": 500.0, "speed_kph": 60.0}]
    assert match_pv_pairs(ref, cur, 1000.0, 1000.0) == []


def test_relative_position_tolerance():
    """同一弯道在两圈的绝对距离不同（漂移），相对位置接近即配对。"""
    ref = [{"kind": "peak", "distance": 900.0, "speed_kph": 200.0}]   # 90/95=94.7%
    cur = [{"kind": "peak", "distance": 920.0, "speed_kph": 195.0}]   # 92.0%
    # 位置差 2.7% → 超容差不配
    assert match_pv_pairs(ref, cur, 950.0, 1000.0) == []
    # 位置差 0.7% → 配对
    cur2 = [{"kind": "peak", "distance": 940.0, "speed_kph": 195.0}]  # 94.0%
    pairs = match_pv_pairs(ref, cur2, 950.0, 1000.0)
    assert len(pairs) == 1
    assert pairs[0]["delta"] == pytest.approx(-5.0)


def test_one_sided_points_not_in_pairs():
    """只在一圈检出的点不进入配对表（避免一堆无意义的 — 行）。"""
    ref = [{"kind": "peak", "distance": 500.0, "speed_kph": 200.0},
           {"kind": "peak", "distance": 900.0, "speed_kph": 220.0}]
    cur = [{"kind": "peak", "distance": 502.0, "speed_kph": 198.0}]
    pairs = match_pv_pairs(ref, cur, 1000.0, 1000.0)
    assert len(pairs) == 1
    assert pairs[0]["distance"] == 500.0


def test_zero_totals_safe():
    ref = [{"kind": "peak", "distance": 500.0, "speed_kph": 200.0}]
    assert match_pv_pairs(ref, [], 0.0, 0.0) == []


# ---------- 集成：analyze_compare 返回 pv_pairs ----------

def _race_frames():
    """三圈合成数据：有加减速（产生峰谷），前圈 + 菜单态被 clean_laps 剔除。"""
    import math
    frames = []
    t = 1000.0
    for lap in (0, 1, 2, 3):
        for i in range(7200):                    # 每圈约 120s @60Hz
            f = i / 7200
            # 两个大起伏：峰 ~250km/h，谷 ~60km/h
            spd = 155 + 95 * math.cos(2 * math.pi * 2 * f)
            frames.append({
                "t": t + i / 60, "lap": lap, "speed_kph": max(20.0, spd),
                "throttle": 0.6, "brake": 0.1,
                "g_force": [0.2, 0.3, 0.0],
                "car_x": 100.0 + i * 0.8, "car_z": 50.0 + 30 * math.sin(f * 4),
            })
        t += 120.0
    frames.append({"t": t, "lap": 65535, "speed_kph": 0.0,
                   "throttle": 0.0, "brake": 0.0,
                   "g_force": [0, 0, 0], "car_x": 0.0, "car_z": 0.0})
    return frames


def test_analyze_compare_returns_pv_pairs():
    r = analyze_compare(_race_frames())
    assert r["laps_analyzed"] >= 2
    assert isinstance(r["pv_pairs"], list)
    for p in r["pv_pairs"]:
        assert p["kind"] in ("peak", "valley")
        assert "delta" in p and "speed_ref" in p and "speed_cur" in p


def test_analyze_compare_pairs_are_sane():
    """所有配对差值必须 |Δ| ≤ 峰谷幅度上限（同类配对防荒谬值）。"""
    r = analyze_compare(_race_frames())
    for p in r["pv_pairs"]:
        assert abs(p["delta"]) < 120.0


def test_empty_frames_pv_pairs():
    r = analyze_compare([])
    assert r["pv_pairs"] == []
    assert clean_laps([]) == {}
