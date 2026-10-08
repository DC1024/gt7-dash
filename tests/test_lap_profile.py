# -*- coding: utf-8 -*-
"""圈剖面（gt7analysis.lap_profile / lap_arc_length）测试。

这是赛道工程师（gt7-coach）的**取数契约**：Dash 把这个剖面发出去，
消费方直接按它建参考圈索引。所以这里守的是"发出去的东西本身是对的"。

用合成圆而不是真数据：合成圆上每个量都能手算，断言能写成等式。
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gt7analysis as A          # noqa: E402
from synth_lap import synth_lap    # noqa: E402

R = 600.0
L = 2.0 * math.pi * R              # ≈ 3769.9 m


def test_arc_length_matches_closed_form():
    """几何弧长必须等于 2πR（弦长替代弧长的误差 ~1e-6 量级）。"""
    pts = A.lap_samples(synth_lap(radius_m=R))
    s, geo_ok = A.lap_arc_length(pts)
    assert geo_ok is True
    assert s[0] == 0.0
    assert s == sorted(s)                      # 单调不减
    # 弦长略短于弧长，允许 0.1%
    assert abs(s[-1] - L) / L < 0.001


def test_profile_reports_both_length_metrics():
    """两种圈长口径都要给出来，且合成数据下两者应当一致。"""
    prof = A.lap_profile(synth_lap(radius_m=R), lap_no=1, step_m=5.0)
    assert "error" not in prof
    assert abs(prof["length_m"] - L) / L < 0.001
    assert abs(prof["length_by_speed_m"] - L) / L < 0.002
    assert abs(prof["length_drift_pct"]) < 0.5
    assert prof["geometry_used"] is True
    assert prof["warnings"] == []
    assert prof["lap"] == 1
    # 圈速 × 平均速度 = 圈长。这是抓单位错误（km/h 当 m/s）最省事的等式：
    # 平均速度必须落在 140~210 km/h（直段 200、减速弯 90 的加权）
    avg_kph = prof["length_m"] / prof["lap_time_s"] * 3.6
    assert 140.0 < avg_kph < 210.0, avg_kph


def test_drift_is_measured_and_warned():
    """速度被抬高 5%、坐标不动 ⇒ 积分口径该多出 ~5%，并给出警告。

    这条是这个端点存在的理由：漂移必须**被量化**，不能悄悄输出。
    """
    prof = A.lap_profile(synth_lap(radius_m=R, speed_scale=1.05),
                         lap_no=1, step_m=10.0)
    assert "error" not in prof
    assert abs(prof["length_m"] - L) / L < 0.001          # 几何不受影响
    assert prof["length_drift_pct"] > 3.0                 # 积分口径偏大
    assert prof["length_by_speed_m"] > prof["length_m"]
    assert any("几何弧长" in w for w in prof["warnings"])


def test_grid_spacing_follows_step():
    prof = A.lap_profile(synth_lap(radius_m=R), lap_no=1, step_m=10.0)
    grid = prof["grid_m"]
    assert len(grid) == pytest.approx(L / 10.0, rel=0.01)
    assert grid[0] == 0.0
    assert all(abs(b - a - 10.0) < 1e-6 for a, b in zip(grid, grid[1:]))
    # 每个通道与网格一一对齐（前端按下标取，错位就整体串台）
    for key in ("speed_kph", "throttle", "brake", "t_rel_s", "glat", "glon"):
        assert len(prof[key]) == len(grid), key
    assert len(prof["pt"]["x"]) == len(grid)
    assert len(prof["pt"]["z"]) == len(grid)


def test_brake_zone_and_apex_detected():
    """刹车区起点应落在减速弯起点附近，弯心落在最低速处。"""
    prof = A.lap_profile(synth_lap(radius_m=R), lap_no=1, step_m=5.0)
    zones = prof["markers"]["brake_in"]
    assert len(zones) == 1, zones
    z = zones[0]
    # 减速从 400 m 开始；阈值 brake>=0.2 会晚一点点，给 40 m 容差
    assert abs(z["s_in_m"] - 400.0) < 40.0, z
    assert z["peak_brake"] > 0.5
    assert z["s_out_m"] > z["s_in_m"]
    assert z["v_min_kph"] == pytest.approx(90.0, abs=3.0)

    apexes = prof["markers"]["apex"]
    assert len(apexes) >= 1
    apex = min(apexes, key=lambda a: a["speed_kph"])
    assert apex["speed_kph"] == pytest.approx(90.0, abs=3.0)
    assert abs(apex["s_m"] - (400.0 + 80.0)) < 40.0      # 谷底在中点
    # 圆的半径是 150 m；由 κ = G_lat·g/v² 反推应当接近
    assert apex["radius_m"] == pytest.approx(R, rel=0.25)
    assert apex["turn"] in ("左", "右")


def test_throttle_on_after_apex():
    prof = A.lap_profile(synth_lap(radius_m=R), lap_no=1, step_m=5.0)
    ton = prof["markers"]["throttle_on"]
    assert len(ton) >= 1
    apex_s = min(a["s_m"] for a in prof["markers"]["apex"])
    for t in ton:
        assert t["after_apex_m"] >= 0
        assert t["s_m"] >= apex_s


def test_missing_coords_degrades_loudly():
    """没坐标时必须**明说降级**，不能假装还是几何口径。

    （老教训：静默降级 = 用户永远不知道自己看的是哪套数据。）
    """
    prof = A.lap_profile(synth_lap(radius_m=R, coords=False), lap_no=1)
    assert "error" not in prof
    assert prof["geometry_used"] is False
    assert prof["pt"]["x"] == [] and prof["pt"]["z"] == []
    assert any("无坐标" in w for w in prof["warnings"])
    # 退化到积分口径，但仍然要有长度
    assert prof["length_m"] > 0


def test_too_few_frames_is_rejected():
    prof = A.lap_profile(synth_lap(radius_m=R)[:10], lap_no=1)
    assert "error" in prof
    assert "frames" in prof


def test_short_lap_is_rejected():
    """圈长 < 200 m（停车场/菜单态残片）不给剖面，而不是给一堆垃圾关键点。"""
    prof = A.lap_profile(synth_lap(radius_m=10.0), lap_no=1)
    assert "error" in prof
    assert prof["length_m"] < 200.0


def test_max_points_caps_grid():
    """小步长 + 长赛道要自动放大步长，否则一次响应能到几 MB。"""
    prof = A.lap_profile(synth_lap(radius_m=R), lap_no=1, step_m=1.0,
                         max_points=50)
    assert "error" not in prof
    assert len(prof["grid_m"]) <= 51
    assert prof["step_m"] > 1.0
