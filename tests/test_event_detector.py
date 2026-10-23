# -*- coding: utf-8 -*-
"""gt7-event-detector.py —— 接入 dashboard 前的 bug 修复回归测试。

盯死四个修复点（见 README/模块内 🔴 注释）：
  1. filter_sustained 尊重调用方 Thresholds.sample_hz（旧版读 DEFAULT，
     改 Thresholds 不生效）；
  2. 滑移率带符号且前后轴分标定半径（旧版绝对值分不出空转/抱死，
     且硬编 0.33 引入 ~1.3% 偏置）；
  3. detect_tyre_abuse 发 TYRE_ABUSE（旧版冒充 SPIN）；
  4. detect_offtrack 判据用 |滑移率| 均值（带符号均值在草地上正负抵消）。
"""
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import load_module

det = load_module("gt7_event_detector_under_test", "gt7-event-detector.py")

R = 0.34          # 测试用前后轴同半径，方便手算滑移率
DT = 1.0 / 60.0   # 60Hz


def _wheels_for(speed_kph, factor=1.0):
    """自由滚动轮速（rad/s），factor>1 空转、<1 抱死。"""
    v = speed_kph / 3.6
    w = v / R
    return [w * factor] * 4


def _mk(i, speed_kph=100.0, factor=1.0, lat_g=0.0, thr=0.0, brk=0.0,
        gear=3, t0=1000.0):
    return det.Sample(
        t=t0 + i * DT, seq=i, speed_kph=speed_kph, gear=gear,
        wheel_speed=_wheels_for(speed_kph, factor),
        g_force=[0.0, lat_g, 0.0], throttle=thr, brake=brk, lap_count=1,
    )


def _run(n, **kw):
    return [_mk(i, **kw) for i in range(n)]


# ---------------------------------------------------------------------------
# 1. filter_sustained 尊重 sample_hz
# ---------------------------------------------------------------------------

def test_filter_sustained_respects_sample_hz():
    """同样 20 帧满足条件：60Hz 下 0.33s 不够 0.4s，25Hz 下 0.8s 够。"""
    s = _run(20, factor=1.3)
    assert det.filter_sustained(s, lambda x: True, min_duration=0.4,
                                sample_hz=60.0) == []
    runs = det.filter_sustained(s, lambda x: True, min_duration=0.4,
                                sample_hz=25.0)
    assert runs == [(0, 19)]


def test_thresholds_sample_hz_takes_effect_via_detector():
    """旧 bug 的集成验证：改 Thresholds.sample_hz 必须影响检测时长门槛。

    15 帧 throttle=1.0 持续 0.25s：60Hz 门槛 30 帧不触发；
    Thresholds(sample_hz=25) 门槛 13 帧应触发 HEAVY_THROTTLE。
    """
    s = _run(15, speed_kph=100.0, thr=1.0)
    base = det.EventDetector()  # 默认 60Hz
    assert base.detect_heavy_throttle(s) == []
    custom = det.EventDetector(th=det.Thresholds(sample_hz=25.0))
    evs = custom.detect_heavy_throttle(s)
    assert len(evs) == 1 and evs[0].type is det.EventType.HEAVY_THROTTLE


# ---------------------------------------------------------------------------
# 2. 带符号滑移 + 分轴半径
# ---------------------------------------------------------------------------

def test_slip_signed_distinguishes_spin_and_lockup():
    """空转 → 正滑移；抱死 → 负滑移。旧版绝对值会把抱死也算成打滑。"""
    v = 100.0 / 3.6
    spin = det.Sample(t=0.0, seq=0, speed_kph=100.0,
                      wheel_speed=[v / R * 1.5] * 4)
    lock = det.Sample(t=0.0, seq=0, speed_kph=100.0,
                      wheel_speed=[v / R * 0.5] * 4)
    assert all(s > 0.49 for s in det.slip_ratios(spin, R, R))
    assert all(s < -0.49 for s in det.slip_ratios(lock, R, R))
    # max_slip（带符号最大值）不会把抱死当打滑
    assert det.max_slip(lock, R, R) < 0.0


def test_front_rear_axes_use_separate_radii():
    """四轮同轮速：前轴 R=0.30 滑移 0，后轴按 R=0.34 换算 → +13.3%。

    若共用一个半径，两轴不可能同时归零——这正是必须分轴标定的原因。
    """
    v = 100.0 / 3.6
    s = det.Sample(t=0.0, seq=0, speed_kph=100.0,
                   wheel_speed=[v / 0.30] * 4)
    slips = det.slip_ratios(s, 0.30, 0.34)
    assert abs(slips[0]) < 1e-9 and abs(slips[1]) < 1e-9
    assert abs(slips[2] - (0.34 / 0.30 - 1.0)) < 1e-9   # 后轮比车快 → 正
    assert abs(slips[3] - (0.34 / 0.30 - 1.0)) < 1e-9


def test_radii_validation_rejects_nonpositive():
    with pytest.raises(ValueError):
        det.EventDetector(radii=(0.0, 0.30))
    with pytest.raises(ValueError):
        det.EventDetector(radii=(0.34, -0.30))


def test_detector_helpers_inject_radii():
    """EventDetector._slip/_mean_slip 走构造时注入的半径。"""
    v = 100.0 / 3.6
    s = det.Sample(t=0.0, seq=0, speed_kph=100.0,
                   wheel_speed=[v / 0.30] * 4)
    d = det.EventDetector(radii=(0.30, 0.30))
    assert all(abs(x) < 1e-9 for x in d._slip(s))
    assert abs(d._mean_slip(s)) < 1e-9
    d0 = det.EventDetector()   # 不注入 → 兜底 0.33 → 后轮"空转"假信号
    assert abs(d0._mean_slip(s) - (0.33 / 0.30 - 1.0)) < 1e-9


# ---------------------------------------------------------------------------
# 3. detect_spin：用注入半径 + max_slip
# ---------------------------------------------------------------------------

def test_detect_spin_uses_injected_radii():
    """横向G 1.3 + 后轴空转 25% 持续 0.5s → SPIN，证据指向标定半径。

    slips = [0, 0, .25, .25]，slips.index(max) 取第一个最大值 → 左前。
    （证据只关心「最严重滑移率」数值，并列轮子的先后顺序无物理意义。）
    """
    s = _run(30, factor=1.25, lat_g=1.3)
    d = det.EventDetector(radii=(R, R))
    evs = d.detect_spin(s)
    assert len(evs) == 1 and evs[0].type is det.EventType.SPIN
    assert evs[0].evidence["打滑最严重轮胎"] == "左前"
    assert abs(evs[0].evidence["该轮滑移率"] - 0.25) < 1e-3


# ---------------------------------------------------------------------------
# 4. detect_offtrack：|滑移率| 判据（负滑移抱死也要能出界）
# ---------------------------------------------------------------------------

def test_detect_offtrack_abs_catches_lockup():
    """40kph 全轮抱死 50%：带符号均值 −0.5 会被旧判据漏掉，abs 判据要抓到。"""
    s = _run(30, speed_kph=40.0, factor=0.5)
    d = det.EventDetector(radii=(R, R))
    evs = d.detect_offtrack(s)
    assert len(evs) == 1 and evs[0].type is det.EventType.OFF_TRACK
    assert abs(evs[0].evidence["平均|滑移率|"] - 0.5) < 1e-3


def test_detect_offtrack_not_triggered_at_speed():
    """高速抱死不判出界（offtrack_max_speed=70）。"""
    s = _run(30, speed_kph=150.0, factor=0.5)
    d = det.EventDetector(radii=(R, R))
    assert d.detect_offtrack(s) == []


# ---------------------------------------------------------------------------
# 5. detect_tyre_abuse：新事件类型
# ---------------------------------------------------------------------------

def test_tyre_abuse_emits_tyre_abuse_not_spin():
    """单轮显著高滑移 + 速度>80 持续 0.5s → TYRE_ABUSE（旧版冒充 SPIN）。"""
    v = 100.0 / 3.6
    s = []
    for i in range(30):
        w = v / R
        s.append(det.Sample(t=1000.0 + i * DT, seq=i, speed_kph=100.0,
                            gear=4, wheel_speed=[w, w, w * 1.5, w]))
    d = det.EventDetector(radii=(R, R))
    evs = d.detect_tyre_abuse(s)
    assert len(evs) == 1
    assert evs[0].type is det.EventType.TYRE_ABUSE
    assert evs[0].type is not det.EventType.SPIN
    assert evs[0].evidence["打滑轮胎"] == "左后"


# ---------------------------------------------------------------------------
# 6. run_all：TYRE_ABUSE 进优先级表
# ---------------------------------------------------------------------------

def test_run_all_priority_table_has_tyre_abuse():
    """TYRE_ABUSE 必须在 priority 表里（缺了排序会静默落到默认 1）。"""
    v = 100.0 / 3.6
    s = []
    for i in range(30):
        w = v / R
        s.append(det.Sample(t=1000.0 + i * DT, seq=i, speed_kph=100.0,
                            gear=4, wheel_speed=[w, w, w * 1.5, w]))
    d = det.EventDetector(radii=(R, R))
    evs = d.run_all(s)
    assert any(e.type is det.EventType.TYRE_ABUSE for e in evs)


# ---------------------------------------------------------------------------
# 5. 压路肩（B4c）：靠格式 C 的 surfaceType 逐轮判定
# ---------------------------------------------------------------------------


def _curb_samples(seq, speed=120.0):
    """按给定路面字符序列造帧（其余量保持"在跑"的状态）。"""
    out = []
    for i, surf in enumerate(seq):
        s = _mk(i, speed_kph=speed)
        s.surface = surf
        out.append(s)
    return out


class TestCurbSlap:
    """`surface` 是 4 个字符（FL/FR/RL/RR）：T 铺装 / C 路肩 / G 草地。

    实测 21175 帧真实 C 包：TTTT 90.2% / GGGG 5.5% / TCTC 1.5% /
    CTCT 1.0% / CCCC 0.2%，TCTC 与 CTCT 成对出现（压右/压左路肩）。
    """

    def test_压一下就走的抖动不报(self):
        """连续不足 curb_min_duration（0.2s ≈ 12 帧）不算一次压路肩。"""
        seq = ["TTTT"] * 30 + ["TCTC"] * 5 + ["TTTT"] * 30
        assert det.EventDetector().detect_curb(_curb_samples(seq)) == []

    def test_持续压路肩报一次(self):
        seq = ["TTTT"] * 30 + ["TCTC"] * 40 + ["TTTT"] * 30
        evs = det.EventDetector().detect_curb(_curb_samples(seq))
        assert len(evs) == 1
        e = evs[0]
        assert e.type == det.EventType.TANK_SLAP
        assert e.evidence["压路肩轮数"] == 2
        assert e.evidence["路面"] == "TCTC"
        assert e.duration > 0.5

    def test_四轮齐上置信度更高(self):
        one = det.EventDetector().detect_curb(
            _curb_samples(["TTTT"] * 20 + ["TCTT"] * 30 + ["TTTT"] * 20))[0]
        four = det.EventDetector().detect_curb(
            _curb_samples(["TTTT"] * 20 + ["CCCC"] * 30 + ["TTTT"] * 20))[0]
        assert four.confidence > one.confidence
        assert four.evidence["压路肩轮数"] == 4

    def test_低速压路肩不算(self):
        """回场圈/维修区压上去没有意义，报出来只会淹没时间线。"""
        seq = ["TTTT"] * 20 + ["TCTC"] * 40 + ["TTTT"] * 20
        assert det.EventDetector().detect_curb(
            _curb_samples(seq, speed=10.0)) == []

    def test_只有草地时不报压路肩(self):
        """纯出界交给 OFF_TRACK，别在这里重复报一遍。"""
        seq = ["TTTT"] * 20 + ["GGGG"] * 40 + ["TTTT"] * 20
        assert det.EventDetector().detect_curb(_curb_samples(seq)) == []

    def test_路肩混草地会标注(self):
        seq = ["TTTT"] * 20 + ["CGCG"] * 40 + ["TTTT"] * 20
        evs = det.EventDetector().detect_curb(_curb_samples(seq))
        assert len(evs) == 1
        assert evs[0].evidence.get("同时压到草地") is True

    def test_A_包没有路面数据时一条都不报(self):
        """🔴 关键：没有数据 ≠ 没压过。

        A/B/~ 包不带 surface，必须返回空列表；如果这里改成"默认 TTTT"，
        A 包场次会永远显示"零次压路肩"，把没数据伪装成开得干净。
        """
        assert det.EventDetector().detect_curb(_run(120)) == []

    def test_run_all_里会跑(self):
        seq = ["TTTT"] * 30 + ["TCTC"] * 40 + ["TTTT"] * 30
        types = {e.type for e in det.EventDetector().run_all(_curb_samples(seq))}
        assert det.EventType.TANK_SLAP in types
