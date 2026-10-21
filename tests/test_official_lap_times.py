# -*- coding: utf-8 -*-
"""官方圈速（last_lap_ms）的还原与优先级测试（P0 / A1）。

GT7 的 `last_lap_ms` 在**冲线后的第一帧**更新，所以第 N 圈的官方用时
要去第 N+1 圈的首帧找。帧跨度会把暂停/进站的墙上时间算进去
（实测第 11 圈 182s，官方 127s），因此分析层必须优先用官方值。
"""
import json

import pytest

from gt7analysis import analyze_compare, official_lap_times


def _lap_frames(lap, n, t0, last_lap_ms=None, dt=1.0):
    out = []
    for i in range(n):
        fr = {"t": t0 + i * dt, "lap": lap, "speed_kph": 100.0, "rpm": 5000.0}
        if i == 0 and last_lap_ms is not None:
            fr["last_lap_ms"] = last_lap_ms
        out.append(fr)
    return out


class TestOfficialLapTimes:
    def test_maps_to_next_lap_first_frame(self):
        laps = {
            1: _lap_frames(1, 3, 0.0, last_lap_ms=99999),
            2: _lap_frames(2, 3, 10.0, last_lap_ms=110000),
            3: _lap_frames(3, 3, 20.0, last_lap_ms=105500),
        }
        # 第 1 圈的值在 2 的首帧；第 2 圈在 3 的首帧；第 3 圈没有下一圈
        assert official_lap_times(laps) == {1: 110.0, 2: 105.5}

    def test_skips_invalid_values(self):
        laps = {
            1: _lap_frames(1, 3, 0.0),
            2: _lap_frames(2, 3, 10.0, last_lap_ms=0),
            3: _lap_frames(3, 3, 20.0, last_lap_ms=120000),
        }
        # 第 1 圈的下一个首帧是 0（无效）→ 不给；第 2 圈是 120s
        assert official_lap_times(laps) == {2: 120.0}

    def test_tolerates_one_frame_delay(self):
        laps = {
            1: _lap_frames(1, 3, 0.0),
            2: _lap_frames(2, 3, 10.0),          # 首帧还没更新
            3: _lap_frames(3, 3, 20.0, last_lap_ms=98000),
        }
        laps[2][1]["last_lap_ms"] = 97000     # 第二帧才有值
        assert official_lap_times(laps)[1] == 97.0


def _write_session(path, frames):
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "x", "started_at": 0.0}) + "\n")
        for fr in frames:
            f.write(json.dumps(fr, separators=(",", ":")) + "\n")


def test_analyze_session_prefers_official_time(dash, tmp_path):
    """帧跨度 180s（含暂停）、官方 120s → 圈速表应显示 120s。"""
    frames = _lap_frames(1, 3, 1000.0)                 # t 1000,1001,1002
    frames[-1]["t"] = 1180.0                            # 人为拉出 178s 跨度
    frames += _lap_frames(2, 3, 1180.1, last_lap_ms=120000)
    p = tmp_path / "20261022_000000_T.jsonl"
    _write_session(p, frames)

    st = dash.analyze_session(p)
    row = next(l for l in st["laps"] if l["lap"] == 1)
    assert row["time"] == pytest.approx(120.0)
    assert row["span_s"] == pytest.approx(180.0)
    assert row["time_source"] == "official"


def test_analyze_session_falls_back_to_span(dash, tmp_path):
    """没有官方值（老场次 / 最后一圈）→ 仍用帧跨度，并标出来源。"""
    frames = _lap_frames(1, 3, 1000.0)
    frames[-1]["t"] = 1180.0
    p = tmp_path / "20261022_000001_T.jsonl"
    _write_session(p, frames)

    st = dash.analyze_session(p)
    row = next(l for l in st["laps"] if l["lap"] == 1)
    assert row["time"] == pytest.approx(180.0)
    assert row["time_source"] == "span"


def test_analyze_compare_uses_official_for_fastest():
    """第 1 圈跨度 89s（含暂停）但官方 50s → 应被选为最快圈。"""
    frames = _lap_frames(1, 90, 0.0)                    # 跨度 89s
    frames += _lap_frames(2, 60, 100.0, last_lap_ms=50000)
    frames += _lap_frames(3, 55, 200.0, last_lap_ms=60000)

    r = analyze_compare(frames)
    assert r["ref_lap"] == 1, r["lap_summary"]
    row = next(s for s in r["lap_summary"] if s["lap"] == 1)
    assert row["duration_s"] == pytest.approx(50.0)
    assert row["span_s"] == pytest.approx(89.0)
    assert row["time_source"] == "official"


def test_analyze_session_last_lap_uses_dropped_final_lap(dash, tmp_path):
    """末圈（完赛离场）会被 clean_laps 丢掉，但它的首帧带着上一圈的官方值。

    回归：之前用清洗后的分组算官方值，最后一场比赛的最后完整圈取不到，
    退回帧跨度（106.609 vs 官方 106.517）。
    """
    frames = _lap_frames(1, 60, 0.0)
    tail = _lap_frames(2, 5, 100.0, last_lap_ms=90000)
    for f in tail:
        f["speed_kph"] = 0.0          # 离场滑行 → clean_laps 会判 stalled 丢掉
    frames += tail
    p = tmp_path / "20261022_000002_T.jsonl"
    _write_session(p, frames)

    st = dash.analyze_session(p)
    row = next(l for l in st["laps"] if l["lap"] == 1)
    assert row["time"] == pytest.approx(90.0)
    assert row["time_source"] == "official"
