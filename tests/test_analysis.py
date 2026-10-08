"""gt7analysis 纯函数测试 —— 用合成数据，断言数学性质。"""
import math

from gt7analysis import (analyze_compare, find_peaks_valleys, lap_samples,
                         race_line, resample_series, split_laps, time_diff)


def make_lap_frames(lap, n=600, spd_kph=150.0, brake_zone=None):
    """匀速圆周展开成直线距离的合成圈；brake_zone=(d0,d1) 区间刹车。"""
    frames = []
    t0 = 1000.0
    dist = 0.0
    for i in range(n):
        dt = 1 / 60
        spd = spd_kph
        brake = 0.0
        throttle = 0.6
        if brake_zone and brake_zone[0] <= dist < brake_zone[1]:
            spd = spd_kph * 0.6
            brake = 0.9
            throttle = 0.0
        v = spd / 3.6
        dist += v * dt
        frames.append({
            "t": t0 + i * dt, "lap": lap, "speed_kph": spd,
            "throttle": throttle, "brake": brake,
            "g_force": [-0.5 if brake else 0.2, 0.3, 0.0],
            "car_x": 100 + dist, "car_z": 50.0,
        })
    return frames


class TestSplitLaps:
    def test_grouping_and_skip_invalid(self):
        frames = (make_lap_frames(1, 100) + make_lap_frames(2, 120)
                  + [{"t": 1, "lap": 0, "speed_kph": 5}])
        laps = split_laps(frames)
        assert set(laps) == {1, 2}
        assert len(laps[1]) == 100 and len(laps[2]) == 120


class TestLapSamples:
    def test_distance_monotonic_and_duration(self):
        pts = lap_samples(make_lap_frames(1, 600, 180.0))
        ds = [p["dist"] for p in pts]
        assert all(b >= a for a, b in zip(ds, ds[1:])), "距离必须单调不减"
        assert pts[-1]["t_rel"] > 9.0, "600帧@60Hz ≈ 10秒"


class TestTimeDiff:
    def test_slower_lap_loses_time_everywhere(self):
        ref = lap_samples(make_lap_frames(1, 600, 180.0))     # 快圈
        cur = lap_samples(make_lap_frames(2, 600, 162.0))     # 慢 10%
        r = time_diff(cur, ref, step=20.0)
        grid = r["grid"]
        assert len(grid) > 5
        # diff = t_ref - t_cur？ 定义：正 = 本圈在该处落后（慢）。
        mid = [d for d, m in zip(grid, r["diff_ms"]) if 200 <= d <= 500]
        vals = [m for d, m in zip(grid, r["diff_ms"]) if 200 <= d <= 500]
        assert all(v > 0 for v in vals), "慢圈在整段距离上应持续为正（丢时间）"


class TestPeaksValleys:
    def test_finds_brake_valley(self):
        pts = lap_samples(make_lap_frames(1, 1200, 180.0, brake_zone=(300, 500)))
        pv = find_peaks_valleys(pts, prominence_kph=10.0)
        kinds = [x["kind"] for x in pv]
        assert "peak" in kinds and "valley" in kinds


class TestRaceLine:
    def test_brake_zone_colored_red(self):
        pts = lap_samples(make_lap_frames(1, 1200, 180.0, brake_zone=(300, 500)))
        seg = race_line(pts)
        colors = {s["color"] for s in seg["segments"]}
        assert {"brake", "throttle"} <= colors or {"brake", "coast"} <= colors

    def test_pedal_arrays_aligned_with_points(self):
        """每段必须带与 pts 等长的 b[]/t[]，前端才能逐点插值上渐变。"""
        pts = lap_samples(make_lap_frames(1, 1200, 180.0, brake_zone=(300, 500)))
        segs = race_line(pts)["segments"]
        assert segs, "至少要有一段"
        for s in segs:
            assert len(s["b"]) == len(s["pts"]) == len(s["t"]), "b/t/pts 必须一一对齐"
            assert all(0.0 <= v <= 1.0 for v in s["b"] + s["t"]), "开度必须规整到 0~1"
        brake_segs = [s for s in segs if s["color"] == "brake"]
        assert brake_segs, "刹车区应产生 brake 段"
        assert max(max(s["b"]) for s in brake_segs) > 0.7, "重刹段应保留高刹车开度"

    def test_segments_share_boundary_point(self):
        """相邻段要共享边界点，否则换色处会出现肉眼可见的缺口。"""
        pts = lap_samples(make_lap_frames(1, 1200, 180.0, brake_zone=(300, 500)))
        segs = race_line(pts)["segments"]
        for a, b in zip(segs, segs[1:]):
            assert a["pts"][-1] == b["pts"][0], "换色处首尾必须相接"

    def test_clamp01_tolerates_percent_and_garbage(self):
        from gt7analysis import _clamp01
        assert _clamp01(0.42) == 0.42
        assert _clamp01(85) == 0.85, "误传百分数要兼容"
        assert _clamp01(-3) == 0.0 and _clamp01(999) == 1.0
        assert _clamp01(None) == 0.0 and _clamp01("x") == 0.0


class TestAnalyzeCompare:
    def test_facade_shape(self):
        frames = make_lap_frames(1, 600, 180.0) + make_lap_frames(2, 600, 175.0)
        r = analyze_compare(frames)
        assert r["ref_lap"] == 1            # 最快圈做参考
        assert r["laps_analyzed"] == 2
        assert r["time_diff"]["grid"]
