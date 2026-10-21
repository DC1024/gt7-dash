# -*- coding: utf-8 -*-
"""B2：三处「最快圈」口径契约测试。

同一场次里，以下三个数字必须指向同一圈：
  · `analyze_session()` 的 `best_lap.lap`
  · `analyze_compare()` 的 `ref_lap`（详情页对比卡的参考圈）
  · `sector_times()` 的 `actual_best_lap`（分段卡的实际最快圈）

它们分别走「圈速表 / 圈间对比 / 分段」三条代码路径。任何一条单独改口径
（例如只给其中一处换官方圈速、或只给其中一处换距离基准），这里立刻红。
"""
import json
import math

import pytest

from gt7analysis import sector_times


def _circle_lap(lap: int, n: int, t0: float, radius: float = 100.0,
                last_lap_ms: int | None = None) -> list[dict]:
    """一圈绕圆；速度按几何弦长/dt 设置，保证几何与积分同量级。"""
    dt = 0.02
    chord = 2 * math.pi * radius / n
    v_ms = chord / dt
    out = []
    for i in range(n):
        ang = 2 * math.pi * i / n
        fr = {
            "t": t0 + i * dt, "lap": lap, "speed_kph": v_ms * 3.6,
            "rpm": 5000.0, "throttle": 0.5, "brake": 0.0,
            "car_x": radius * math.cos(ang), "car_z": radius * math.sin(ang),
            "g_force": [0.0, 0.0, 0.0],
        }
        if i == 0 and last_lap_ms is not None:
            fr["last_lap_ms"] = last_lap_ms
        out.append(fr)
    return out


def _write_session(path, frames):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "x", "started_at": 0.0}) + "\n")
        for fr in frames:
            f.write(json.dumps(fr, separators=(",", ":")) + "\n")


def test_three_fastest_lap_paths_agree(dash, tmp_path):
    """3 个完整圈 + 1 个离场圈；第 3 圈有暂停（span 40s、官方 22s）。

    官方圈速：1=24.0、2=23.5、3=22.0。按帧跨度第 3 圈最慢（40s），
    按官方圈速它最快——三处都必须选第 3 圈。
    """
    t = 0.0
    frames = _circle_lap(1, 1500, t)
    t += 30.0
    frames += _circle_lap(2, 1500, t, last_lap_ms=24000)
    t += 30.0
    frames += _circle_lap(3, 2000, t, last_lap_ms=23500)   # 40s，含暂停
    t += 40.0
    frames += _circle_lap(4, 5, t, last_lap_ms=22000)       # 离场圈
    for fr in frames[-5:]:
        fr["speed_kph"] = 0.0

    p = tmp_path / "20261022_010000_T.jsonl"
    _write_session(p, frames)

    st = dash.analyze_session(p)
    cmp_ = dash.compare_session(p)
    sec = dash.session_sectors(p)

    best = st.get("best_lap") or {}
    assert best.get("lap") == 3, st.get("laps")
    assert best.get("time") == pytest.approx(22.0)
    assert cmp_.get("ref_lap") == 3, cmp_.get("lap_summary")
    assert sec.get("actual_best_lap") == 3, sec.get("laps")

    # 三处连「秒数」也必须是同一个官方值（不是帧跨度）
    assert sec.get("actual_best_s") == pytest.approx(22.0)
    row = next(l for l in sec["laps"] if l["lap"] == 3)
    assert row["time_source"] == "official"
    assert row["total_s"] == pytest.approx(22.0)
    assert row["span_s"] == pytest.approx(40.0, abs=0.05)


def test_sector_reference_ignores_clock_jump_lap():
    """2 圈：一圈距离是另一圈的 3 倍（时钟跳变），不能被当成基准。

    回归：sector_times 之前用普通中位数，2 圈时取较大的那个，
    把 3 倍距离的假圈当基准，真实圈被判 partial、最快圈报错。
    """
    def flat_lap(lap, n_sectors, t0):
        """无坐标、按速度积分构造；每段 100 帧、每帧 10m。"""
        frames = []
        t = t0
        for _ in range(n_sectors):
            # dt 必须 <1.0：lap_samples 的积分守卫是 0.001 < dt < 1.0，
            # 正好 1.0 会被跳过、距离恒为 0（第一版就踩了这个坑）。
            dt = 0.5
            v = 10.0 / dt
            for _ in range(100):
                frames.append({"t": t, "lap": lap, "speed_kph": v * 3.6,
                               "throttle": 0.0, "brake": 0.0,
                               "g_force": [0.0, 0.0, 0.0]})
                t += dt
        frames.append({"t": t, "lap": lap, "speed_kph": 0.0,
                       "throttle": 0.0, "brake": 0.0, "g_force": [0.0, 0.0, 0.0]})
        return frames

    laps = {
        1: flat_lap(1, 4, 0.0),        # 4000m / 400s
        2: flat_lap(2, 12, 1000.0),    # 12000m / 1200s（3 倍，时钟跳变）
    }
    # 两圈的 span 都远大于官方用时 → 都应被排除出「基准候选」
    st = sector_times(laps, official={1: 80.0, 2: 45.0})

    assert st["partial_laps"] == [2]
    assert st["actual_best_lap"] == 1
    assert st["actual_best_s"] == pytest.approx(80.0)
