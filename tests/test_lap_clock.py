# -*- coding: utf-8 -*-
"""「本圈已用时」的口径测试。

坑：原先 `lap_time = latest.t − session_start` 算出来的是**场次已用时**，
不随圈重置。主界面那个圈速大计时器（`$('laptime')`）跑第 3 圈时显示的
是三圈的累计时间，冲线不归零；v1 的 `current_lap_time_s` 文档写着
「本圈已用时」，也对不上。接收器补上 `lap_started_at` 之后才对。

这里守两件事：
  1. 有 `lap_started_at` 时必须用它（且冲线后归零）
  2. 没有时必须**明说是兜底口径**，不能假装自己是真圈时
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


def _status(path: Path, *, frame_t: float, session_start: float,
            lap_started_at: float | None) -> Path:
    payload = {
        "t": time.time(), "recording": True,
        "frame": {"t": frame_t, "speed_kph": 180.0, "lap": 3,
                  "car_x": 1.0, "car_z": 1.0},
        "history": [], "session_start": session_start,
        "frames_total": 10, "layouts": {}, "warning": None,
        "has_coords": True, "session_max_speed": 200.0,
        "path": [], "gg": [], "lap_times": [], "lap_fuel": [],
        "powertrain": "fuel", "max_energy_recovery": 0.0,
        "source_ips": [], "ps5_filter": "auto", "grid_start": 0,
    }
    if lap_started_at is not None:
        payload["lap_started_at"] = lap_started_at
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture
def hub(dash, tmp_path):
    def _make(**kw):
        p = tmp_path / "status.json"
        _status(p, **kw)
        h = dash.TelemetryHub(p)
        h.refresh()
        return h
    return _make


class TestLapClock:
    def test_uses_lap_started_at(self, hub):
        """场次已跑 300 s、本圈才跑 12.5 s —— 必须给 12.5，不是 300。"""
        h = hub(frame_t=1000.0, session_start=700.0, lap_started_at=987.5)
        snap = h.snapshot()
        assert snap["lap_time"] == pytest.approx(12.5, abs=0.01)
        assert snap["lap_time_source"] == "lap"

    def test_resets_at_lap_line(self, hub):
        """刚冲线那一帧：（几乎）归零。这是这个字段存在的全部意义。"""
        h = hub(frame_t=1070.0, session_start=700.0, lap_started_at=1070.0)
        assert h.snapshot()["lap_time"] == pytest.approx(0.0, abs=0.001)

    def test_old_recorder_falls_back_and_says_so(self, hub):
        """老记录器不写 lap_started_at → 退回场次口径，但必须标注出来。

        静默给一个看起来正常的错数字，比报错难查一百倍。
        """
        h = hub(frame_t=1000.0, session_start=700.0, lap_started_at=None)
        snap = h.snapshot()
        assert snap["lap_time"] == pytest.approx(300.0, abs=0.01)
        assert snap["lap_time_source"] == "session"

    def test_zero_lap_started_at_not_treated_as_valid(self, hub):
        """0 不是合法时刻（Unix epoch 起点），必须走兜底分支。

        写成 `payload.get("lap_started_at", session_start)` 就会让 0 漏进来，
        于是 lap_time = 当前时刻 − 0 ≈ 17 亿秒。
        """
        h = hub(frame_t=1000.0, session_start=700.0, lap_started_at=0.0)
        snap = h.snapshot()
        assert snap["lap_time_source"] == "session"
        assert snap["lap_time"] < 1000.0

    def test_negative_guard(self, hub):
        """圈起点晚于当前帧（时钟回拨/写文件竞争）时夹到 0，不给负数。"""
        h = hub(frame_t=1000.0, session_start=700.0, lap_started_at=1001.0)
        assert h.snapshot()["lap_time"] == 0.0

    def test_v1_exposes_source(self, dash, hub):
        v1 = dash._v1_live(hub(frame_t=1000.0, session_start=700.0,
                               lap_started_at=987.5).snapshot())
        t = v1["timing"]
        assert t["current_lap_time_s"] == pytest.approx(12.5, abs=0.01)
        assert t["current_lap_time_source"] == "lap"

    def test_lap_started_at_not_sticky_across_sessions(self, dash, tmp_path):
        """换场次后旧值必须清掉。

        「缺失时沿用上一次」是这类字段最容易犯的错：换场后 lap_time 会
        拿新场次的帧时刻去减上一场的圈起点，算出一个荒谬的大数。
        """
        p = tmp_path / "status.json"
        _status(p, frame_t=1000.0, session_start=700.0, lap_started_at=987.5)
        h = dash.TelemetryHub(p)
        h.refresh()
        assert h.snapshot()["lap_time_source"] == "lap"
        _status(p, frame_t=1000.0, session_start=990.0, lap_started_at=None)
        h.refresh()
        assert h.snapshot()["lap_time_source"] == "session"
        assert h._lap_started_at == 0.0
