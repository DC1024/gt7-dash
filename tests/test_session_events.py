# -*- coding: utf-8 -*-
"""驾驶事件时间线（dashboard 适配层）—— session_events 回归测试。

合成两圈场次盯死：
  1. 滑移类事件（SPIN）从检测器出来，圈号 / 时间对得上，radii 注入生效；
  2. OFF_TRACK 几何层主导：坐标往外偏 10m 的段要变成出界事件（src=geometry）；
  3. 单圈场次偏差不可靠（ref==cmp）→ 退回滑移判据（src=slip）；
  4. 检测器文件缺失 → available=False，不是 500。
"""
import json
import math

from pathlib import Path

import pytest

RF, RR = 0.34, 0.345          # 与 test_sector_slip 同一套真实半径
DT = 1.0 / 60.0


def _write_session(path: Path, frames) -> None:
    path.write_text(
        json.dumps({"header": True}) + "\n"
        + "".join(json.dumps(fr) + "\n" for fr in frames),
        encoding="utf-8",
    )


def _circle_xy(i, radius=100.0, samples=1500):
    a = 2 * math.pi * i / samples
    return math.cos(a) * radius, math.sin(a) * radius


def _frame(i, lap, v_ms=27.78, kf=1.0, kr=1.0, lat_g=0.0,
           thr=0.0, brk=0.0, off_m=0.0):
    x, z = _circle_xy(i, radius=100.0 + off_m)
    return {
        "t": i * DT, "lap": lap, "speed_kph": v_ms * 3.6, "rpm": 5000.0,
        "gear": 3, "throttle": thr, "brake": brk,
        "g_force": [0.0, lat_g, 0.0],
        "car_x": x, "car_z": z,
        "wheel_rads": [kf * v_ms / RF, kf * v_ms / RF,
                       kr * v_ms / RR, kr * v_ms / RR],
    }


def _two_lap_frames(spin_at=None, offtrack_at=None):
    """两圈合成场次：第 2 圈可插空转段（spin）与外偏段（off_track）。"""
    def _lap(lap):
        frames = []
        for i in range(1500):
            kw = {}
            # 空转 / 外偏段只插在第 2 圈：第 1 圈当干净的参考圈
            if lap == 2:
                if spin_at and spin_at[0] <= i < spin_at[1]:
                    kw = dict(kr=1.3, lat_g=1.3, thr=1.0)
                if offtrack_at and offtrack_at[0] <= i < offtrack_at[1]:
                    kw["off_m"] = 10.0
            frames.append(_frame(i, lap, **kw))
        return frames

    frames = _lap(1) + _lap(2)
    # 第 2 圈时间接在第 1 圈后面（clean_laps / deviation 都要求 t 单调）
    for fr in frames[1500:]:
        fr["t"] += 1500 * DT
    return frames


def test_spin_event_and_radii_injection(dash, tmp_path):
    f = tmp_path / "20260101_120000_unknown.jsonl"
    # 空转段放在第 2 圈的第 720~750 帧（不贴圈边界，deviation 才稳）
    _write_session(f, _two_lap_frames(spin_at=(720, 750)))

    out = dash.session_events(f)
    assert out["available"] is True
    # 标定半径被还原并注入（自由滚动帧足够）
    c = out["calibration"]
    assert c["available"] is True
    assert c["front_m"] == pytest.approx(RF, abs=1e-3)
    assert c["rear_m"] == pytest.approx(RR, abs=1e-3)

    spins = [e for e in out["events"] if e["type"] == "spin"]
    assert len(spins) == 1
    e = spins[0]
    assert e["lap"] == 2
    # 第 2 圈从全局第 1500 帧开始 → 圈内 t_rel ≈ 720×DT
    assert e["t_rel"] == pytest.approx(720 * DT, abs=2 * DT)
    assert e["src"] == "telemetry"
    assert e["evidence"]["峰值横向G"] == pytest.approx(1.3, abs=0.01)
    assert e["evidence"]["该轮滑移率"] == pytest.approx(0.3, abs=0.02)


def test_offtrack_geometry_dominates(dash, tmp_path):
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(offtrack_at=(600, 700)))

    out = dash.session_events(f)
    offs = [e for e in out["events"] if e["type"] == "off_track"]
    assert len(offs) == 1
    e = offs[0]
    assert e["src"] == "geometry"
    assert e["lap"] == 2
    assert e["evidence"]["最大横向偏移_m"] == pytest.approx(10.0, abs=1.0)
    # 100 帧外偏 ≈ 1.67s
    assert e["t_end_rel"] - e["t_rel"] == pytest.approx(100 * DT, abs=6 * DT)
    # 滑移判据不应同时报（速度 100kph > offtrack_max_speed=70）
    assert not any(x["src"] == "slip" for x in offs)


def test_single_lap_falls_back_to_slip(dash, tmp_path):
    """单圈场次 deviation 的 ref==cmp → 不可靠 → 退回「低速+高滑移」。"""
    frames = [_frame(i, 1, v_ms=11.11, kf=0.5, kr=0.5, brk=1.0)
              if 300 <= i < 360 else _frame(i, 1)
              for i in range(1500)]
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, frames)

    out = dash.session_events(f)
    offs = [e for e in out["events"] if e["type"] == "off_track"]
    assert len(offs) == 1
    assert offs[0]["src"] == "slip"
    assert offs[0]["lap"] == 1


def test_detector_file_missing_degrades(dash, tmp_path, monkeypatch):
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames())
    monkeypatch.setattr(dash, "_DETECTOR_MOD", None)
    monkeypatch.setattr(dash, "_DETECTOR_TRIED", True)
    out = dash.session_events(f)
    assert out["available"] is False
    assert "gt7-event-detector.py" in out["reason"]


def test_single_lap_scope(dash, tmp_path):
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(spin_at=(720, 750)))
    out = dash.session_events(f, lap_no=1)
    assert out["laps"] == [1]
    assert all(e["lap"] == 1 for e in out["events"])
    assert not any(e["type"] == "spin" for e in out["events"])
