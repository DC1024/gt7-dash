# -*- coding: utf-8 -*-
"""v1.4.0：比赛语义事件（超车 / 进站 / 最快圈）。

这一批补上了 10 类事件里缺的 3 类。三者的判据都**不是**靠猜：

  · 超车 —— 遥测 `quali_pos`（0x84）在比赛中就是实时名次。
    社区文档把它写作 "RaceStartPosition"（发车位），是错的：
    14 场真实数据里它在 12→2→8→11→14→4 循环。
  · 进站 —— GT7 不广播 pit 状态，只能从「油量上升」+「持续低速」推断，
    两者置信度不同，不能一视同仁。
  · 最快圈 —— 用 GT7 官方 `last_lap_ms`，不用帧跨度（后者会把暂停算进去）。

这里盯死几个**最容易写错**的地方：
  1. `65535` / `0` 是「没有名次」的哨兵，不滤掉会产出"从 P0 超到 P3"；
  2. 名次闪一下（掉帧、瞬时哨兵）不是超车，必须防抖；
  3. 进站期间掉的名次不是被超 —— 必须在算超车前排除掉；
  4. 进站的"仅低速"弱证据必须降置信度，且证据里写清可能是事故。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import load_module

det = load_module("gt7_race_events_under_test", "gt7-event-detector.py")

DT = 1.0 / 60.0


def _mk(n, *, t0=0.0, speed=200.0, pos=None, cars=20, gas=30.0, cap=60.0,
        throttle=0.8, lap=1):
    """造 n 帧。pos 可以是常量、list（逐帧）、或 None（无名次）。"""
    out = []
    for i in range(n):
        p = pos[i] if isinstance(pos, (list, tuple)) else (pos or 0)
        out.append(det.Sample(
            t=t0 + i * DT, seq=i, speed_kph=speed, rpm=6000.0, gear=4,
            lap_count=lap, throttle=throttle, brake=0.0,
            quali_pos=p, num_cars=cars,
            gas_level=gas, gas_capacity=cap,
        ))
    return out


def _pos_series(seq, **kw):
    """[(pos, 帧数), ...] → 逐帧名次列表。"""
    out = []
    for p, n in seq:
        out.extend([p] * n)
    return _mk(len(out), pos=out, **kw)


class TestOvertakeFromTelemetry:
    def test_名次提升算一次超车(self):
        s = _pos_series([(5, 120), (4, 120)])
        evs = det.EventDetector().detect_position_changes(s)
        assert len(evs) == 1
        e = evs[0]
        assert e.type == det.EventType.OVERTAKE
        assert e.evidence["名次"] == "P5 → P4"
        assert e.evidence["方向"].startswith("提升")
        assert e.confidence == pytest.approx(0.9)   # 干净的单次超越

    def test_名次下降算被超(self):
        s = _pos_series([(4, 120), (5, 120)])
        evs = det.EventDetector().detect_position_changes(s)
        assert len(evs) == 1
        assert evs[0].evidence["方向"].startswith("下降")
        assert evs[0].evidence["变化"] == "-1"

    def test_65535_哨兵被过滤(self):
        """计时赛/菜单里 quali_pos = 65535。不滤掉就会算出 P65535→P5。"""
        s = _pos_series([(65535, 120), (5, 120)])
        assert det.EventDetector().detect_position_changes(s) == []

    def test_0_被过滤(self):
        s = _pos_series([(0, 120), (5, 120)])
        assert det.EventDetector().detect_position_changes(s) == []

    def test_名次超过车数被当作脏数据丢弃(self):
        s = _mk(240, pos=[25] * 120 + [4] * 120, cars=20)
        assert det.EventDetector().detect_position_changes(s) == []

    def test_单帧跳变不产生事件(self):
        """掉帧/瞬时哨兵会让名次闪一下，防抖必须挡住。"""
        pos = [5] * 120 + [4] + [5] * 120      # 只闪 1 帧
        s = _mk(len(pos), pos=pos)
        assert det.EventDetector().detect_position_changes(s) == []

    def test_一次跨多位降置信度(self):
        s = _pos_series([(10, 120), (7, 120)])
        evs = det.EventDetector().detect_position_changes(s)
        assert len(evs) == 1
        assert evs[0].confidence == pytest.approx(0.5), (
            "跨 3 位几乎都是进站/发车混乱，不该给高置信度")

    def test_进站窗口内的掉位被排除(self):
        s = _pos_series([(5, 120), (9, 120)])
        # 变化发生在 t≈2.0s，把它标成进站窗口
        evs = det.EventDetector().detect_position_changes(
            s, exclude_windows=[(1.5, 2.5)])
        assert evs == [], "进站期间掉的名次不是被超"

    def test_无名次场次返回空(self):
        assert det.EventDetector().detect_position_changes(_mk(120)) == []


class TestPitLike:
    def test_加油是强证据(self):
        """低速段里油量上升 → 只有进站可能，给 0.95。"""
        a = _mk(300, speed=200.0, gas=30.0, throttle=0.8)
        b = _mk(600, speed=60.0, gas=30.0, throttle=0.2, t0=300 * DT)
        # 在低速段中间加油 20L
        for i in range(200, 260):
            b[i].gas_level = 50.0
        evs = det.EventDetector().detect_pit_like(a + b)
        assert len(evs) == 1
        assert evs[0].type == det.EventType.PIT_LIKE
        assert evs[0].confidence >= 0.9
        assert "加油" in evs[0].evidence["判据"]

    def test_只有低速没有加油_低置信度(self):
        a = _mk(300, speed=200.0)
        b = _mk(900, speed=55.0, throttle=0.1, t0=300 * DT)   # 15 秒低速
        evs = det.EventDetector().detect_pit_like(a + b)
        assert len(evs) == 1
        assert evs[0].confidence == pytest.approx(0.5)
        assert "也可能是事故" in evs[0].evidence["判据"], (
            "弱证据必须如实说明不确定性，不能假装确定")

    def test_短暂低速不报(self):
        """慢弯会短暂掉到 60km/h 以下，4 秒门槛要挡住它。"""
        a = _mk(120, speed=200.0)
        b = _mk(120, speed=55.0, t0=120 * DT)      # 2 秒
        c = _mk(120, speed=200.0, t0=240 * DT)
        assert det.EventDetector().detect_pit_like(a + b + c) == []

    def test_高速行驶不报(self):
        assert det.EventDetector().detect_pit_like(_mk(600, speed=180.0)) == []


class TestRunAllIncludesNewTypes:
    def test_run_all_会带上超车与进站(self):
        a = _mk(120, speed=200.0, pos=5)
        b = _mk(600, speed=55.0, gas=50.0, throttle=0.15, pos=9, t0=120 * DT)
        evs = det.EventDetector().run_all(a + b)
        types = {e.type for e in evs}
        assert det.EventType.PIT_LIKE in types
        # 掉位发生在进站窗口里 → 不该同时报超车
        assert det.EventType.OVERTAKE not in types, (
            "进站掉的位不能被同时报成「被超」")


class TestPitNotPause:
    """暂停 ≠ 进站。

    实测踩过：一场真实比赛里有两段「速度 < 70 持续 19.7s / 101.3s」，
    平均速度 6.4 / 1.1 km/h —— 那是玩家按了暂停（速度 0，帧照发），
    旧判据把它报成进站，2 次全是误报。
    """

    def test_整段静止不报进站(self):
        a = _mk(300, speed=200.0)
        b = _mk(900, speed=0.0, throttle=0.0, t0=300 * DT)   # 15 秒静止
        assert det.EventDetector().detect_pit_like(a + b) == []

    def test_维修区匀速通行会报(self):
        a = _mk(300, speed=200.0)
        b = _mk(900, speed=60.0, throttle=0.15, t0=300 * DT)
        evs = det.EventDetector().detect_pit_like(a + b)
        assert len(evs) == 1
        assert evs[0].evidence["平均速度_kph"] >= 15.0
