# -*- coding: utf-8 -*-
"""几何弧长作为距离基准（A2）。

速度积分 `Σ v·Δt` 依赖时间戳：丢包/时钟跳变会把圈长放大（实测 5078m
的赛道积出 7066m，把完整圈判成残圈）。几何弧长只依赖 car_x/car_z，
不受时间戳影响。没有坐标时回退积分。
"""
import math

import pytest

from gt7analysis import (_lap_geo_length, lap_distances, lap_samples)


def _circle_frames(n=2000, radius=100.0, dt=0.02, speed_kph=None, lap=1):
    """绕圆一圈；speed_kph=None 时按几何速度（弦长/dt）设置，保证两种口径一致。"""
    out = []
    chord = 2 * math.pi * radius / n
    v_ms = chord / dt if speed_kph is None else speed_kph / 3.6
    for i in range(n):
        ang = 2 * math.pi * i / n
        out.append({
            "t": i * dt, "lap": lap, "speed_kph": v_ms * 3.6,
            "rpm": 5000.0, "throttle": 0.5, "brake": 0.0,
            "car_x": radius * math.cos(ang), "car_z": radius * math.sin(ang),
            "g_force": [0.0, 0.0, 0.0],
        })
    return out


class TestGeoLength:
    def test_circle_length(self):
        frames = _circle_frames()
        assert _lap_geo_length(frames) == pytest.approx(2 * math.pi * 100, rel=0.001)

    def test_no_coords_returns_zero(self):
        frames = [{"t": i * 0.02, "car_x": None, "car_z": None} for i in range(50)]
        assert _lap_geo_length(frames) == 0.0

    def test_coordinate_jump_is_skipped(self):
        frames = [{"car_x": float(i), "car_z": 0.0} for i in range(12)]
        frames[6] = {"car_x": 500.0, "car_z": 0.0}   # 跳变：进出各 1 段都跳过
        # 正常段 5 + 4 = 9m（跳变前后两段不计）
        assert _lap_geo_length(frames) == pytest.approx(9.0)


class TestLapDistancesPrefersGeo:
    def test_uses_geo_when_coords_present(self):
        """速度 300km/h 但坐标只绕了 628m：距离应取几何值，不是积分的 3333m。"""
        laps = {i: _circle_frames(speed_kph=300.0, lap=i) for i in (1, 2, 3)}
        dists, ref = lap_distances(laps)
        assert all(d == pytest.approx(2 * math.pi * 100, rel=0.01)
                   for d in dists.values())
        assert ref == pytest.approx(2 * math.pi * 100, rel=0.01)

    def test_falls_back_without_coords(self):
        frames = [{"t": i * 0.02, "lap": 1, "speed_kph": 100.0} for i in range(100)]
        laps = {1: frames, 2: frames, 3: frames}
        dists, _ = lap_distances(laps)
        assert all(d == pytest.approx(100 / 3.6 * 0.02 * 99, rel=0.01)
                   for d in dists.values())


class TestLapSamplesAxis:
    def test_uses_geo_when_consistent(self):
        pts = lap_samples(_circle_frames())
        assert pts[-1]["dist"] == pytest.approx(2 * math.pi * 100, rel=0.01)

    def test_falls_back_when_coords_scaled(self):
        """坐标缩放异常（几何只有积分的 1/40）→ 回退积分口径。"""
        pts = lap_samples(_circle_frames(speed_kph=300.0))
        assert pts[-1]["dist"] > 3000, pts[-1]["dist"]
