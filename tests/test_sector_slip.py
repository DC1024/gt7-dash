# -*- coding: utf-8 -*-
"""分段计时 / 理论最快圈 与 轮胎滑移检测 —— 回归测试。

两套用例都按真实场次的实测特征构造（数字取自 20261007_231350 那场 LM55 23 圈）：
  - 圈长 ~6940m、圈速 142~256s、4 段
  - 自标定半径 前 0.3391m / 后 0.3435m（比值 0.987）
  - 自由滚动帧滑移 |s| ≈ 0.0005；抱死最低 −1.000；低速空转峰值 +7.98
"""
import math

import pytest

from gt7analysis import sector_times, wheel_slip

# ---------------------------------------------------------------- 分段计时

FRAMES_PER_SECTOR = 100
STEP_M = 10.0            # 每帧固定前进 10m


def _lap_secs(lap: int, sector_times_s: list[float], t0: float) -> list[dict]:
    """构造一圈，各段用时**精确等于** sector_times_s。

    lap_samples 是按「上一帧速度 × dt」积分距离的，所以只要让 speed×dt 恒等于
    STEP_M，累计距离就是 STEP_M 的整数倍、段边界正好落在帧上，插值零误差。
    """
    frames: list[dict] = []
    t = t0
    for st in sector_times_s:
        dt = st / FRAMES_PER_SECTOR
        v = STEP_M / dt                       # m/s（由此 speed×dt ≡ STEP_M）
        for _ in range(FRAMES_PER_SECTOR):
            frames.append({"t": t, "lap": lap, "speed_kph": v * 3.6,
                           "throttle": 0.0, "brake": 0.0,
                           "g_force": [0.0, 0.0, 0.0]})
            t += dt
    # 收尾帧：把最后一段的距离也积出来（总距离 = STEP_M × 段数 × 每段帧数）
    frames.append({"t": t, "lap": lap, "speed_kph": 0.0,
                   "throttle": 0.0, "brake": 0.0, "g_force": [0.0, 0.0, 0.0]})
    return frames


def test_sector_times_exact():
    """段用时必须等于构造值 —— 距离等分 + 线性插值不该引入误差。"""
    laps = {1: _lap_secs(1, [25.0, 30.0, 35.0, 40.0], 1000.0)}
    st = sector_times(laps, n_sectors=4)
    row = st["laps"][0]
    assert row["sectors"] == [25.0, 30.0, 35.0, 40.0]
    assert row["total_s"] == 130.0
    assert row["dist_m"] == 4000.0
    assert st["ref_dist_m"] == 4000.0


def test_single_lap_is_not_reliable():
    """只有 1 个可信圈时不能给理论最快圈 —— 宁可空着也不给会骗人的数字。"""
    st = sector_times({1: _lap_secs(1, [25.0, 30.0, 35.0, 40.0], 1000.0)})
    assert st["reliable"] is False
    assert st["theoretical_best_s"] is None
    assert st["potential_gain_s"] is None
    assert st["actual_best_lap"] == 1
    assert st["actual_best_s"] == 130.0
    assert "不足 2 个" in st["note"]


def test_theoretical_best_is_sum_of_sector_minima():
    """理论最快圈 = 各段全场最快之和；潜在空间 = 实际最快 − 理论最快。"""
    laps = {
        1: _lap_secs(1, [25.0, 30.0, 35.0, 40.0], 1000.0),   # 130s
        2: _lap_secs(2, [24.0, 31.0, 34.0, 39.0], 2000.0),   # 128s ← 最快
        3: _lap_secs(3, [26.0, 29.0, 36.0, 38.0], 3000.0),   # 129s
    }
    st = sector_times(laps, n_sectors=4)
    assert st["reliable"] is True
    assert st["actual_best_lap"] == 2
    assert st["actual_best_s"] == 128.0
    assert st["best_each_s"] == [24.0, 29.0, 34.0, 38.0]
    assert st["theoretical_best_s"] == 125.0
    assert st["potential_gain_s"] == 3.0
    assert st["potential_gain_pct"] == pytest.approx(3.0 / 128.0 * 100, abs=0.01)
    assert st["counted_laps"] == [1, 2, 3]
    assert st["partial_laps"] == []


def test_理论最快圈小于实际最快圈是正常的():
    """⚠️ 防「顺手修成相等」：理论值本来就该小于实际值。

    两圈必须都落在 tol 内（≤120×1.05=126s）才会被统计，
    否则只剩 1 个可信圈、理论值直接为 None。
    """
    laps = {
        1: _lap_secs(1, [30.0, 30.0, 30.0, 30.0], 1000.0),   # 120s ← 实际最快
        2: _lap_secs(2, [20.0, 34.0, 34.0, 34.0], 2000.0),   # 122s，S1 极快
    }
    st = sector_times(laps, n_sectors=4)
    assert st["reliable"] is True
    assert st["actual_best_s"] == 120.0
    assert st["theoretical_best_s"] == 110.0        # 20+30+30+30
    assert st["theoretical_best_s"] < st["actual_best_s"]
    assert st["potential_gain_s"] == 10.0


def test_slow_laps_do_not_poison_theoretical():
    """慢圈（冲出赛道/打转）里的某段可能"恰好很快"，必须靠 tol 挡在外面。"""
    laps = {
        1: _lap_secs(1, [25.0, 30.0, 35.0, 40.0], 1000.0),   # 130s
        2: _lap_secs(2, [24.5, 30.5, 35.5, 39.5], 2000.0),   # 130s
        # 一圈打转的：总时长 200s，但 S1 只有 5s（"恰好很快"）
        3: _lap_secs(3, [5.0, 65.0, 65.0, 65.0], 3000.0),
    }
    st = sector_times(laps, n_sectors=4)
    assert 3 not in st["counted_laps"]
    assert st["counted_laps"] == [1, 2]
    # 各段最快取自 1、2 两圈：24.5 / 30.0 / 35.0 / 39.5（那假的 5.0 被挡掉）
    assert st["best_each_s"] == [24.5, 30.0, 35.0, 39.5]
    assert st["theoretical_best_s"] == 129.0


def test_partial_lap_is_not_chosen_as_actual_best():
    """🔴 残缺圈（没跑满整圈）总时长也偏短，绝不能被选成 actual_best。

    段边界按各圈**自己的**圈长等分，所以只有跑满整圈的圈边界才对得齐。
    实测第 1 圈 6129.6m（最快圈 6937.8m）的 S1 只有 25.2s，
    比全场最快 33.2s 还"快" 8 秒 —— 纯属边界错位。
    """
    laps = {
        1: _lap_secs(1, [25.0, 30.0, 35.0, 40.0], 1000.0),   # 4000m / 130s 完整
        2: _lap_secs(2, [20.0, 25.0, 30.0], 2000.0),         # 3000m /  75s 残缺
    }
    st = sector_times(laps, n_sectors=4)
    rows = {r["lap"]: r for r in st["laps"]}
    # 先证明这个陷阱是真的：残缺圈的总时长确实比"实际最快圈"还短
    assert rows[2]["total_s"] < rows[1]["total_s"]
    # 修复后它被挡在选最快圈之前
    assert st["partial_laps"] == [2]
    assert st["actual_best_lap"] == 1
    assert st["actual_best_s"] == 130.0
    assert rows[2]["partial"] is True
    assert rows[1]["partial"] is False
    # 残缺圈的段边界不在同一段路上，给差值只会误导
    assert "deltas" not in rows[2]


def test_sector_count_is_arbitrary_and_partitions_the_lap():
    """段数可调，且各段用时必须精确铺满整圈（不重不漏）。"""
    laps = {1: _lap_secs(1, [25.0, 30.0, 35.0, 40.0], 1000.0)}
    for n in (2, 3, 4, 6, 8):
        st = sector_times(laps, n_sectors=n)
        row = st["laps"][0]
        assert len(row["sectors"]) == n
        assert sum(row["sectors"]) == pytest.approx(row["total_s"], abs=0.05)


def test_empty_and_degenerate_input():
    """没有可用圈时给 note，不要抛异常。"""
    st = sector_times({})
    assert st["laps"] == []
    assert st["theoretical_best_s"] is None
    assert "没有可用于分段的圈" in st["note"]
    # 帧数不足（采样点 <10）的圈也要被跳过
    st = sector_times({1: [{"t": 1000.0, "lap": 1, "speed_kph": 50.0}]})
    assert st["laps"] == []
    assert "没有可用于分段的圈" in st["note"]


# ---------------------------------------------------------------- 轮胎滑移

RF_TRUE = 0.34        # 前轴真实半径（与实测 0.3391 同量级）
RR_TRUE = 0.345       # 后轴真实半径（比值 0.986，与实测 0.987 一致）


def _slip_lap(lap: int, regions: list[tuple], t0: float = 1000.0,
              dt: float = 1 / 60) -> list[dict]:
    """regions 每项：(帧数, v(m/s), 油门, 刹车, ω前系数, ω后系数)。

    ω = 系数 × v / R_true。系数 = 1 就是自由滚动；<1 轮子转得比车慢（抱死）；
    >1 转得比车快（空转）。系数直接决定滑移率的理论值，便于精确断言。
    """
    frames: list[dict] = []
    t = t0
    for n, v, thr, brk, kf, kr in regions:
        wf = kf * v / RF_TRUE
        wr = kr * v / RR_TRUE
        for _ in range(n):
            frames.append({
                "t": t, "lap": lap, "speed_kph": v * 3.6,
                "throttle": thr, "brake": brk, "g_force": [0.0, 0.0, 0.0],
                "wheel_rads": [wf, wf, wr, wr],
            })
            t += dt
    return frames


def test_calibration_recovers_true_radii():
    """自标定必须还原真实半径，且前后轴分别标定。"""
    out = wheel_slip({1: _slip_lap(1, [(150, 30.0, 0.0, 0.0, 1.0, 1.0)])})
    c = out["calibration"]
    assert out["available"] is True
    assert c["front_m"] == pytest.approx(RF_TRUE, abs=1e-4)
    assert c["rear_m"] == pytest.approx(RR_TRUE, abs=1e-4)
    assert c["front_m"] != c["rear_m"]
    assert c["ratio"] == pytest.approx(RF_TRUE / RR_TRUE, abs=1e-4)
    assert c["free_frames"] == 150
    assert c["ok"] is True
    # 自洽性：自由滚动帧的滑移均值应≈0
    assert abs(c["free_slip_front"]) < 1e-3
    assert abs(c["free_slip_rear"]) < 1e-3


def test_single_radius_would_bias_slip():
    """前后轴若强行用同一个半径，自由滚动帧的滑移均值会被整体偏置。

    这是「必须分别标定」的理由：偏置 ≈ R前/R后 − 1 ≈ −1.4%，
    而抱死的典型信号本身只有百分之几，偏置不可忽略。
    """
    out = wheel_slip({1: _slip_lap(1, [(150, 30.0, 0.0, 0.0, 1.0, 1.0)])})
    c = out["calibration"]
    bias = c["front_m"] / c["rear_m"] - 1.0
    assert bias == pytest.approx(RF_TRUE / RR_TRUE - 1, abs=1e-4)
    assert bias < -0.01


def test_lockup_and_wheelspin_detected_with_correct_sign():
    """抱死给负滑移、空转给正滑移，且各聚成一次事件（不是一帧一次）。"""
    out = wheel_slip({1: _slip_lap(1, [
        (120, 30.0, 0.0, 0.0, 1.0, 1.0),    # 自由滚动 → 标定用
        (10, 20.0, 0.0, 1.0, 0.0, 1.0),     # 前轮 ω=0 → 滑移 −1.000（全锁）
        (10, 10.0, 1.0, 0.0, 1.0, 1.5),     # 后轮快 50% → 滑移 +0.500（空转）
    ])})
    assert out["available"] is True

    assert out["lockup"]["events"] == 1
    assert out["lockup"]["frames"] == 10
    worst_lock = out["lockup"]["worst"][0]
    assert worst_lock["slip"] == pytest.approx(-1.0, abs=1e-6)
    assert worst_lock["brake"] == 100.0
    assert worst_lock["lap"] == 1

    assert out["wheelspin"]["events"] == 1
    assert out["wheelspin"]["frames"] == 10
    worst_spin = out["wheelspin"]["worst"][0]
    assert worst_spin["slip"] == pytest.approx(0.5, abs=1e-6)
    assert worst_spin["throttle"] == 100.0

    # 逐圈统计的极值也要对得上
    row = out["laps"][0]
    assert row["front"]["min"] == pytest.approx(-1.0, abs=1e-6)
    assert row["rear"]["max"] == pytest.approx(0.5, abs=1e-6)
    assert row["lockup_frames"] == 10
    assert row["wheelspin_frames"] == 10


def test_single_frame_spike_is_not_an_event():
    """1 帧的尖峰不算一次事件（否则噪声会被报成几十次抱死）。"""
    frames = _slip_lap(1, [
        (120, 30.0, 0.0, 0.0, 1.0, 1.0),
        (1, 20.0, 0.0, 1.0, 0.0, 1.0),      # 只有一帧全锁
    ])
    out = wheel_slip({1: frames})
    assert out["lockup"]["events"] == 0


def test_low_speed_frames_do_not_count_as_slip():
    """低速帧要排除：v→0 时滑移率会被除出噪声。"""
    out = wheel_slip({1: _slip_lap(1, [
        (120, 30.0, 0.0, 0.0, 1.0, 1.0),
        (30, 1.0, 1.0, 0.0, 1.0, 5.0),      # 1m/s 全油门狂转，但低于 _SLIP_MIN_V
    ])})
    assert out["wheelspin"]["frames"] == 0


def test_series_is_capped_per_lap():
    """曲线抽稀按「每圈目标点数」封顶，输出体积不随帧数膨胀。"""
    frames = _slip_lap(1, [(1000, 30.0, 0.0, 0.0, 1.0, 1.0)])
    out = wheel_slip({1: frames}, max_per_lap=120)
    assert out["max_per_lap"] == 120
    assert 0 < len(out["series"][1]["t"]) <= 120
    for k in ("t", "front", "rear", "speed_kph", "throttle", "brake"):
        assert len(out["series"][1][k]) == len(out["series"][1]["t"])


def test_missing_wheel_rads_degrades_cleanly():
    """缺 wheel_rads 的老场次（含合成测试数据）要整卡隐藏，不能给空表。"""
    frames = [{"t": 1000.0 + i / 60, "lap": 1, "speed_kph": 100.0,
               "throttle": 0.0, "brake": 0.0, "g_force": [0.0, 0.0, 0.0]}
              for i in range(200)]
    out = wheel_slip({1: frames})
    assert out["available"] is False
    assert "wheel_rads" in out["reason"]


def test_no_free_rolling_frames_rejects_calibration():
    """全程没有自由滚动帧 → 标定必须拒绝，而不是硬给一个半径。"""
    out = wheel_slip({1: _slip_lap(1, [(200, 30.0, 1.0, 0.0, 1.0, 1.0)])})
    assert out["available"] is False
    assert out["calibration"]["free_frames"] == 0
    assert "自由滚动帧" in out["reason"]


def test_slip_empty_input():
    assert wheel_slip({})["available"] is False


def test_multi_lap_series_and_stats_are_per_lap():
    """多圈要各算各的：series 按圈分开，事件带正确圈号。"""
    laps = {
        1: _slip_lap(1, [(120, 30.0, 0.0, 0.0, 1.0, 1.0)], t0=1000.0),
        2: _slip_lap(2, [(120, 30.0, 0.0, 0.0, 1.0, 1.0),
                         (10, 20.0, 0.0, 1.0, 0.0, 1.0)], t0=2000.0),
    }
    out = wheel_slip(laps)
    assert sorted(out["series"]) == [1, 2]
    assert out["lockup"]["events"] == 1
    assert out["lockup"]["worst"][0]["lap"] == 2
    assert [r["lap"] for r in out["laps"]] == [1, 2]
    assert [r["lockup_frames"] for r in out["laps"]] == [0, 10]


def test_slip_uses_both_lateral_and_longitudinal_g_for_free_roll():
    """自由滚动判据要同时看纵向与横向 G —— 弯中不是自由滚动。

    只看一个通道会把弯中（横向 G 大）的帧误当自由滚动，污染半径标定。
    """
    frames = _slip_lap(1, [(150, 30.0, 0.0, 0.0, 1.0, 1.0)])
    for f in frames:
        f["g_force"] = [0.0, 0.9, 0.0]      # 横向 0.9g（弯中），纵向为 0
    out = wheel_slip({1: frames})
    assert out["available"] is False          # 一帧自由滚动都没有
    assert out["calibration"]["free_frames"] == 0


def test_slip_rate_formula_matches_definition():
    """s = (ω·R − v)/v —— 用标定出的半径手算一遍核对。"""
    out = wheel_slip({1: _slip_lap(1, [
        (120, 30.0, 0.0, 0.0, 1.0, 1.0),
        (10, 20.0, 1.0, 0.0, 1.0, 2.0),     # 后轮转两倍 → s = +1.000
    ])})
    c = out["calibration"]
    v = 20.0
    wr = 2.0 * v / RR_TRUE
    expect = (wr * c["rear_m"] - v) / v
    assert out["wheelspin"]["worst"][0]["slip"] == pytest.approx(expect, abs=1e-3)
    assert expect == pytest.approx(1.0, abs=1e-4)
    assert math.isfinite(expect)
