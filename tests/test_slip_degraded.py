# -*- coding: utf-8 -*-
"""B1：标定不可信时，滑移数据必须**降级**，而不是照常输出精确数字。

背景：自洽性检查（标定后自由滚动帧的滑移均值应≈0）不通过，说明半径本身
就错了，滑移率会整体平移一个未知量。图表看着完全正常、数值却是错的。

旧行为：前端只挂一句「⚠ 未通过」，数字照旧给 —— 读者看到一张有具体数值
的图，会直接采信。
新行为：`degraded: true` + `degrade_reason`，且**不再输出"最严重的一次
抱死/空转"排序**（排序建立在错的滑移率上，同样是错的），前端整块弱化。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gt7analysis as ga

DT = 1.0 / 60.0
R = 0.34


def _laps(n_laps=2, frames=600, free=True):
    """造多圈数据。free=True 时全部是自由滚动帧（标定应自洽）。"""
    laps, t = {}, 1000.0
    for ln in range(1, n_laps + 1):
        fs = []
        for i in range(frames):
            v = 40.0 / 3.6                      # 恒定 40 km/h
            w = v / R
            fs.append({
                "t": t + i * DT, "lap": ln, "speed_kph": 40.0,
                "throttle": 0.0 if free else 0.9,
                "brake": 0.0,
                "wheel_rads": [w, w, w, w],
                "g_force": [0.0, 0.0, 0.0],
            })
        laps[ln] = fs
        t += frames * DT
    return laps


class TestSlipDegraded:
    def test_标定自洽时不降级(self):
        out = ga.wheel_slip(_laps())
        assert out["available"] is True
        assert out["degraded"] is False
        assert "degrade_reason" not in out

    def test_标定不自洽时降级(self, monkeypatch):
        def fake(free, total):
            return 0.34, 0.34, {
                "ok": False, "reason": "自由滚动帧滑移均值偏 3.2%",
                "front_m": 0.34, "rear_m": 0.34, "ratio": 1.0,
                "free_frames": len(free), "free_pct": 100.0,
                "free_slip_front": 0.032, "free_slip_rear": 0.031,
            }
        monkeypatch.setattr(ga, "_calibrate_radii_from_free", fake)
        out = ga.wheel_slip(_laps())
        assert out["available"] is True
        assert out["degraded"] is True
        assert "3.2%" in out["degrade_reason"]

    def test_降级时不输出最严重事件排序(self, monkeypatch):
        """排序建立在错的滑移率上，同样是错的 —— 不能当结论展示。"""
        monkeypatch.setattr(ga, "_calibrate_radii_from_free", lambda f, t: (
            0.34, 0.34, {"ok": False, "reason": "污染", "front_m": 0.34,
                         "rear_m": 0.34, "ratio": 1.0, "free_frames": 999,
                         "free_pct": 100.0, "free_slip_front": .03,
                         "free_slip_rear": .03}))
        out = ga.wheel_slip(_laps())
        assert out["lockup"]["worst"] == []
        assert out["wheelspin"]["worst"] == []
        # 但计数仍在：用户仍能知道"这一圈有 3 帧抱死"，只是不该被排名
        assert "events" in out["lockup"]

    def test_没有轮速数据时仍是不可用(self):
        laps = _laps()
        for fs in laps.values():
            for f in fs:
                f.pop("wheel_rads")
        out = ga.wheel_slip(laps)
        assert out["available"] is False
