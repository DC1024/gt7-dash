#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GT7 POV 解说工具 — Phase 1核心：遥测事件检测
=================================================
从 GT7 UDP 遥测（或 .gtz 回放文件）检测驾驶事件，为解说提供时间锚点。

设计原则：
  1. 事实来自遥测，不猜。所有阈值都是物理量或经验值，可解释。
  2. 事件 = (时间窗, 类型, 置信度, 证据)，证据里是可复核的遥测数字。
  3. 复盘分析基于「与最佳圈的赛道坐标对齐对比」，而不是简单的圈速差。

数据来源：
  - GT7 广播 UDP 33739（高低位轮速）/ 33740（Addendum2，含全部字段）
  - 或 .gtz / .gtr 回放文件（gttelemetry 库可读）

依赖：仅标准库。Python 3.10+
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# 遥测采样点
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """单帧遥测采样。字段名对齐 GT7 Addendum2 协议（经 gttelemetry 整理）。"""
    t: float                  # 时间戳（秒）
    seq: int                  # 序列号

    # 运动
    speed_kph: float = 0.0        # 车速 km/h
    rpm: float = 0.0              # 发动机转速
    gear: int = 0                 # 档位，0=空档，-1=倒挡，-2=空挡状态
    car_x: float = 0.0            # 车身坐标 X（用于映射赛道位置）
    car_z: float = 0.0            # 车身坐标 Z
    lap_count: int = 0            # 当前圈数

    # 踏板 0..1
    throttle: float = 0.0
    brake: float = 0.0
    # 离合器：踩下=1（手动挡换挡）
    clutch: float = 0.0

    # 转向
    steer_angle: float = 0.0      # 方向盘角度，度

    # 四轮（顺序 FL, FR, RL, RR）
    wheel_speed: list[float] = field(default_factory=lambda: [0.0] * 4)   # rad/s
    tyre_temp: list[float] = field(default_factory=lambda: [0.0] * 4)     # ℃
    tyre_press: list[float] = field(default_factory=lambda: [0.0] * 4)    # kPa
    tyre_wear: list[float] = field(default_factory=lambda: [0.0] * 4)     # %

    # 悬挂 / 车身
    susp_height: list[float] = field(default_factory=lambda: [0.0] * 4)  # mm
    g_force: list[float] = field(default_factory=lambda: [0.0] * 3)      # [纵向, 横向, 垂直] g

    # 元信息（解码得到，可能为 None）
    circuit: str | None = None
    car_name: str | None = None


# ---------------------------------------------------------------------------
# 事件定义
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    """事件类型。value 是给 LLM 看的中文描述。"""
    HARD_BRAKING = "hard_braking"       # 极限刹车
    COLLISION = "collision"             # 碰撞
    SPIN = "spin"                       # 打滑/失控
    TYRE_ABUSE = "tyre_abuse"           # 轮胎滥用（单轮滑移显著异于其他）
    OVERTAKE = "overtake"               # 超车（未实现，见 README）
    LAP_RECORD = "lap_record"           # 个人最快圈（未实现）
    PIT_LIKE = "pit_like"               # 进站（未实现）
    HEAVY_THROTTLE = "heavy_throttle"   # 大油门出弯
    OFF_TRACK = "off_track"             # 出界
    TANK_SLAP = "tank_slap"             # 压路肩石/草地（未实现）


@dataclass
class Event:
    """一个检测到的驾驶事件。"""
    t_start: float
    t_end: float
    type: EventType
    confidence: float              # 0~1
    lap: int
    evidence: dict[str, Any]       # 可复核的遥测证据
    comment_hint: str = ""         # 给 LLM 的提示（不含结论，让 LLM 去组织语言）

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


# ---------------------------------------------------------------------------
# 阈值配置（全部可调，按自己的口味改）
# ---------------------------------------------------------------------------

@dataclass
class Thresholds:
    # 打滑/失控：横向G 超阈值 且 滑移率 高
    spin_lat_g: float = 1.05
    spin_slip_ratio: float = 0.14      # 轮速 vs 车速 换算的滑移率

    # 碰撞：纵向G 强负（减速）
    impact_long_g: float = -2.2

    # 极限刹车：刹车深且车速高
    hard_brake_throttle: float = 0.85
    hard_brake_min_speed: float = 130.0   # km/h

    # 出界：低速 + 大量滑移
    offtrack_max_speed: float = 70.0
    offtrack_slip: float = 0.22

    # 轮胎打滑：单轮转速显著高于其他
    wheel_slip_delta: float = 0.18

    # 超车判定：与参考圈的赛道坐标差（归一化）
    # 车头（car_z）和车尾（car_x）同时落后参考 → 说明在被超
    overtake_margin: float = 0.0022

    # 大油门出弯：出弯速度区间的全油门
    heavy_throttle_min_speed: float = 60.0    # km/h
    heavy_throttle_max_speed: float = 150.0   # km/h；上限把直道全油门排除在外

    # 采样率（GT7 广播约 60Hz，这里按 60Hz 处理）
    sample_hz: float = 60.0


DEFAULT = Thresholds()


# ---------------------------------------------------------------------------
# 滑移率计算
# ---------------------------------------------------------------------------

# 轮胎半径（米）→ 用于把 rad/s 换成 m/s。
# 🔴 这只是**兜底默认值**。真实标定必须用自由滚动帧最小二乘
#    （gt7analysis.calibrate_wheel_radii，前后轴**分别**标定——实测
#    R前 0.3391 / R后 0.3435 m，比值 0.987）。EventDetector(radii=(rf, rr))
#    注入标定值；不注入时滑移率会被整体偏置约 1.3%，而抱死的典型信号
#    本身只有百分之几，偏置不可忽略。
_TYRE_RADIUS_M = 0.33


def wheel_linear_speed(wheel_rad_s: float, radius: float = _TYRE_RADIUS_M) -> float:
    """单轮线速度 m/s。"""
    return wheel_rad_s * radius


def vehicle_speed_ms(speed_kph: float) -> float:
    """车速 km/h → m/s。"""
    return speed_kph / 3.6


def slip_ratios(s: Sample, radius_front: float | None = None,
                radius_rear: float | None = None) -> list[float]:
    """四轮滑移率（**带符号**），顺序 FL/FR/RL/RR。

    🔴 定义与 /slip（gt7analysis.wheel_slip）同一套：s = (ω·R − v) / v，
       v>0 才有意义，v≈0 时恒 0。**正 = 轮子转得比车快（空转/打滑）**，
       **负 = 比车慢（抱死）**——旧版取绝对值，分不出空转和抱死，
       会让 SPIN 事件把刹车抱死也算进去，别改回去。

    🔴 前轮用 radius_front、后轮用 radius_rear（None 则回退兜底值
       _TYRE_RADIUS_M）。GT7 前后胎规格常不同，必须分轴标定。
    """
    rf = _TYRE_RADIUS_M if radius_front is None else radius_front
    rr = _TYRE_RADIUS_M if radius_rear is None else radius_rear
    v = vehicle_speed_ms(s.speed_kph)
    out = []
    for i, w in enumerate(s.wheel_speed):
        vs = wheel_linear_speed(w, rf if i < 2 else rr)
        out.append((vs - v) / v if v > 0 else 0.0)
    return out


def abs_slip_ratios(s: Sample, radius_front: float | None = None,
                    radius_rear: float | None = None) -> list[float]:
    """|滑移率|——只关心「有没有滑」，不关心方向的场景用（如出界检测）。"""
    return [abs(x) for x in slip_ratios(s, radius_front, radius_rear)]


def mean_slip(s: Sample, radius_front: float | None = None,
              radius_rear: float | None = None) -> float:
    """四轮**带符号**滑移率的均值。

    注意：均值会稀释单轮打滑、且正负会互相抵消；判「打滑最严重的轮子」
    请配合 max_slip / abs_slip_ratios 使用。
    """
    return sum(slip_ratios(s, radius_front, radius_rear)) / 4.0


def max_slip(s: Sample, radius_front: float | None = None,
             radius_rear: float | None = None) -> float:
    """单轮最大**正**滑移率（= 最严重的空转/打滑方向）。

    比 mean_slip 更能反映「某一侧轮胎失去抓地」这种真实失控形态：
    真实赛道上四轮同时打滑罕见，平均值会把这个信号抹平。
    """
    return max(slip_ratios(s, radius_front, radius_rear))


def lateral_g(s: Sample) -> float:
    return abs(s.g_force[1]) if len(s.g_force) >= 2 else 0.0


def longitudinal_g(s: Sample) -> float:
    return s.g_force[0] if s.g_force else 0.0


def vertical_g(s: Sample) -> float:
    return s.g_force[2] if len(s.g_force) >= 3 else 0.0


# ---------------------------------------------------------------------------
# 基础分析
# ---------------------------------------------------------------------------

def find_lap_boundaries(samples: list[Sample]) -> list[tuple[int, int, float]]:
    """
    切分各圈。返回 [(起始索引, 结束索引, 该圈用时)]。
    依据 lap_count 变化 + 起终点位置突变（回到起点附近）。

    ⚠️ 已知局限：只看 lap_count 变化，**不剔除假圈**——前圈（开局排队
       静止）、末圈（完赛滑行离场）、菜单态（lap=0xFFFF）都会被当成圈。
       dashboard 集成**不用这个函数**：那边用 clean_laps（按圈长偏离
       中位数判假）分组好再喂进来。独立使用本模块时请注意这个差别。
    """
    if not samples:
        return []

    laps: list[tuple[int, int, float]] = []
    start_idx = 0
    current_lap = samples[0].lap_count

    for i in range(1, len(samples)):
        if samples[i].lap_count != current_lap:
            laps.append((start_idx, i - 1, samples[i - 1].t - samples[start_idx].t))
            start_idx = i
            current_lap = samples[i].lap_count

    if start_idx < len(samples) - 1:
        laps.append((start_idx, len(samples) - 1, samples[-1].t - samples[start_idx].t))

    return laps


def filter_sustained(
    samples: list[Sample],
    predicate: Any,
    min_duration: float = 0.25,
    sample_hz: float | None = None,
) -> list[tuple[int, int]]:
    """
    找出 predicate 连续成立且持续 >= min_duration 的区间。
    predicate: Sample -> bool
    返回 [(起始索引, 结束索引)]
    这样能过滤掉瞬时噪声尖峰。

    🔴 sample_hz 必须传调用方实际配置的采样率（EventDetector 会传
       self.th.sample_hz）。旧版在这里读模块级 DEFAULT.sample_hz，
       导致改 Thresholds 里的采样率不生效——别改回去。
    """
    hz = DEFAULT.sample_hz if sample_hz is None else sample_hz
    min_frames = max(2, int(min_duration * hz))
    runs: list[tuple[int, int]] = []
    run_start = None

    for i, s in enumerate(samples):
        if predicate(s):
            if run_start is None:
                run_start = i
        else:
            if run_start is not None:
                if i - run_start >= min_frames:
                    runs.append((run_start, i - 1))
                run_start = None

    if run_start is not None and len(samples) - run_start >= min_frames:
        runs.append((run_start, len(samples) - 1))

    return runs


# （time_gaps 已删除：为「不连续数据块分段」而写，从未被任何检测器
#   调用过——死代码就删掉，git 历史里找得回来。）


# ---------------------------------------------------------------------------
# 事件检测器
# ---------------------------------------------------------------------------

class EventDetector:
    """从遥测序列中检测驾驶事件。

    radii: 可选 (front_m, rear_m) 标定半径——来自
    gt7analysis.calibrate_wheel_radii 的自由滚动帧最小二乘结果。
    不传则全部轮子用兜底值 _TYRE_RADIUS_M（滑移率整体偏置 ~1.3%，
    只适合没有 wheel_rads 标定条件的场合）。
    """

    def __init__(self, th: Thresholds | None = None,
                 radii: tuple[float, float] | None = None):
        self.th = th or DEFAULT
        if radii is not None:
            rf, rr = radii
            if rf <= 0 or rr <= 0:
                raise ValueError(f"标定半径必须为正：{radii!r}")
        self.radii = radii

    def _slip(self, s: Sample) -> list[float]:
        """带符号四轮滑移率（用构造时注入的标定半径）。"""
        if self.radii is not None:
            return slip_ratios(s, self.radii[0], self.radii[1])
        return slip_ratios(s)

    def _abs_slip(self, s: Sample) -> list[float]:
        if self.radii is not None:
            return abs_slip_ratios(s, self.radii[0], self.radii[1])
        return abs_slip_ratios(s)

    def _mean_slip(self, s: Sample) -> float:
        if self.radii is not None:
            return mean_slip(s, self.radii[0], self.radii[1])
        return mean_slip(s)

    def _max_slip(self, s: Sample) -> float:
        if self.radii is not None:
            return max_slip(s, self.radii[0], self.radii[1])
        return max_slip(s)

    # -- 单一事件检测 -----------------------------------------------------

    def detect_hard_braking(self, samples: list[Sample]) -> list[Event]:
        """极限刹车：高速下深踩刹车。

        注意：不能只靠「刹车踏板深」+「持续一段时间」两个条件——
        一次长直道末尾的重刹会持续好几秒，但那是正常操作。
        真正的极限刹车应该用「刹车起始时的减速强度」来判定。
        """
        out = []
        th = self.th
        # 找刹车起始点（踏板从松开到深踩的上升沿）
        for i in range(1, len(samples)):
            s = samples[i]
            prev = samples[i - 1]
            if s.brake <= th.hard_brake_throttle or prev.brake > th.hard_brake_throttle:
                continue
            if s.speed_kph < th.hard_brake_min_speed:
                continue

            # 从刹车起始向后看 1.2 秒，测量实际减速强度
            window = int(1.2 * th.sample_hz)
            seg = samples[i : i + window]
            if len(seg) < 5:
                continue

            # 用「速度差 / 时间」算平均减速度，比纵向 G 更可靠
            dv = seg[0].speed_kph - min(x.speed_kph for x in seg)
            dt = seg[-1].t - seg[0].t
            if dt <= 0:
                continue
            decel_kph_s = dv / dt                    # km/h per second
            peak_g = min(longitudinal_g(x) for x in seg)

            # 判定：减速度要够大。25 m/s^2 ≈ 90 km/h 每秒，这是接近 F1 极限刹车水平
            decel_ms2 = decel_kph_s / 3.6
            if decel_ms2 < 4.0:                       # 低于 4 m/s^2 只是普通刹车
                continue

            conf = min(1.0, decel_ms2 / 22.0)

            # 刹车持续时长（到松刹车为止），用于给 LLM 分配解说时长
            end_idx = i
            for j in range(i, len(samples)):
                if samples[j].brake < 0.3:
                    end_idx = j
                    break
            else:
                end_idx = len(samples) - 1

            out.append(
                Event(
                    t_start=samples[i].t,
                    t_end=samples[end_idx].t,
                    type=EventType.HARD_BRAKING,
                    confidence=conf,
                    lap=s.lap_count,
                    evidence={
                        "刹车起始速度_kph": round(s.speed_kph, 1),
                        "平均减速度_m_s2": round(decel_ms2, 1),
                        "峰值刹车踏板": round(max(x.brake for x in seg), 2),
                        "最低纵向G": round(peak_g, 2),
                        "制动持续_s": round(samples[end_idx].t - samples[i].t, 2),
                        "最低点速度_kph": round(min(x.speed_kph for x in seg), 1),
                    },
                    comment_hint="高速重刹，判断是极限刹车点表现；重点看玩家刹得够不够晚",
                )
            )
        return out

    def detect_impact(self, samples: list[Sample]) -> list[Event]:
        """碰撞：**没踩刹车**时的瞬时强纵向减速。

        🔴 判据里必须有「brake 浅」这一条：GT7 极限重刹能到 −3.3g
           （实测本场 hard_braking 最低 −4.36g），只看 G 值会把每次
           重刹都当碰撞（实测 1098 次误报）。碰撞的物理特征是「玩家
           没在刹车，速度却在骤降」——撞墙 / 追尾 / 被撞。
        """
        out = []
        th = self.th.impact_long_g
        i = 1
        while i < len(samples):
            s = samples[i]
            if longitudinal_g(s) < th and s.brake < 0.35:
                # 前后各 0.2s 作窗口
                pre = max(0, i - int(0.2 * self.th.sample_hz))
                post = min(len(samples) - 1, i + int(0.2 * self.th.sample_hz))
                out.append(
                    Event(
                        t_start=samples[pre].t,
                        t_end=samples[post].t,
                        type=EventType.COLLISION,
                        confidence=min(1.0, abs(longitudinal_g(s)) / 5.0),
                        lap=s.lap_count,
                        evidence={
                            "碰撞瞬间速度_kph": round(s.speed_kph, 1),
                            "纵向G": round(longitudinal_g(s), 2),
                            "碰撞前转速_rpm": round(s.rpm),
                            "是否同时踩刹车": bool(s.brake > 0.3),
                        },
                        comment_hint="检测到强烈减速冲击，判断发生了碰撞或蹭墙",
                    )
                )
                i = post
            else:
                i += 1
        return out

    def detect_spin(self, samples: list[Sample]) -> list[Event]:
        """打滑/失控：横向G 大 + 存在明显滑移的轮胎。

        用 max_slip 而非 mean_slip：真实赛道上失控往往是「一侧轮胎失去抓地」，
        四轮平均会把信号稀释到检测不出来。
        """
        out = []
        runs = filter_sustained(
            samples,
            lambda s: lateral_g(s) > self.th.spin_lat_g
            and self._max_slip(s) > self.th.spin_slip_ratio,
            min_duration=0.4,
            sample_hz=self.th.sample_hz,
        )
        for a, b in runs:
            seg = samples[a : b + 1]
            peak = max(seg, key=lateral_g)
            slips = self._slip(peak)
            worst = slips.index(max(slips))
            wheel_names = ["左前", "右前", "左后", "右后"]
            out.append(
                Event(
                    t_start=samples[a].t,
                    t_end=samples[b].t,
                    type=EventType.SPIN,
                    confidence=min(1.0, lateral_g(peak) / 1.8),
                    lap=peak.lap_count,
                    evidence={
                        "峰值横向G": round(lateral_g(peak), 2),
                        "峰值速度_kph": round(peak.speed_kph, 1),
                        "打滑最严重轮胎": wheel_names[worst],
                        "该轮滑移率": round(slips[worst], 3),
                        "四轮平均滑移率": round(self._mean_slip(peak), 3),
                        "方向": round(peak.steer_angle, 1),
                    },
                    comment_hint="车辆处于失控边缘，判断这次转向超出了抓地极限",
                )
            )
        return out

    def detect_offtrack(self, samples: list[Sample]) -> list[Event]:
        """出界：低速 + 高滑移（压草地/路肩）。

        🔴 判据用 |滑移率| 的均值（abs_slip_ratios）：压草地上前轮抱死（负）
        后轮空转（正）很常见，带符号均值会正负抵消把信号抹没。

        注意：dashboard 集成里 OFF_TRACK 由 track_deviation 的 dlat
        （|dlat| > ~5m）主导，本滑移判据只在没有几何参照的场合兜底。
        """
        out = []
        runs = filter_sustained(
            samples,
            lambda s: s.speed_kph < self.th.offtrack_max_speed
            and sum(self._abs_slip(s)) / 4.0 > self.th.offtrack_slip,
            min_duration=0.4,
            sample_hz=self.th.sample_hz,
        )
        for a, b in runs:
            s = samples[a]
            mean_abs = sum(self._abs_slip(s)) / 4.0
            out.append(
                Event(
                    t_start=samples[a].t,
                    t_end=samples[b].t,
                    type=EventType.OFF_TRACK,
                    confidence=min(1.0, mean_abs * 4),
                    lap=s.lap_count,
                    evidence={
                        "速度_kph": round(s.speed_kph, 1),
                        "平均|滑移率|": round(mean_abs, 3),
                        "转向角": round(s.steer_angle, 1),
                    },
                    comment_hint="车辆驶出赛道表面，抓地力大幅下降",
                )
            )
        return out

    def detect_heavy_throttle(self, samples: list[Sample]) -> list[Event]:
        """大油门出弯：出弯速度区间内的持续全油门。

        🔴 速度必须有**上限**：只设 60kph 下限时，直道全油门（可到
           250kph+、每圈几十段）全部命中，实测一场 704 次——时间线
           被淹没，事件失去信息量。加上限后才是「出弯油门点」。
        """
        out = []
        runs = filter_sustained(
            samples,
            lambda s: (s.throttle > 0.95
                       and self.th.heavy_throttle_min_speed < s.speed_kph
                       < self.th.heavy_throttle_max_speed and s.gear >= 2),
            min_duration=0.5,
            sample_hz=self.th.sample_hz,
        )
        for a, b in runs:
            peak = max(samples[a : b + 1], key=lambda s: s.speed_kph)
            out.append(
                Event(
                    t_start=samples[a].t,
                    t_end=samples[b].t,
                    type=EventType.HEAVY_THROTTLE,
                    confidence=0.7,
                    lap=peak.lap_count,
                    evidence={
                        "出弯速度_kph": round(peak.speed_kph, 1),
                        "持续全油门_s": round(samples[b].t - samples[a].t, 2),
                        "转速_rpm": round(peak.rpm),
                    },
                    comment_hint="玩家在出弯时保持全油门，注意轮胎负荷",
                )
            )
        return out

    def detect_tyre_abuse(self, samples: list[Sample]) -> list[Event]:
        """轮胎滥用：单轮滑移率显著高于其他（针对单轮打滑/内外轮差异）。

        🔴 事件类型是 TYRE_ABUSE——旧版错误地发 EventType.SPIN，
           会在时间线上和真正的失控事件混淆，别改回去。
        """
        out = []
        th = self.th.wheel_slip_delta

        def bad(s: Sample) -> bool:
            slips = self._slip(s)
            return max(slips) - min(slips) > th and s.speed_kph > 80

        runs = filter_sustained(
            samples, bad, min_duration=0.3, sample_hz=self.th.sample_hz
        )
        wheel_names = ["左前", "右前", "左后", "右后"]
        for a, b in runs:
            peak = max(samples[a : b + 1], key=lambda s: max(self._slip(s)))
            slips = self._slip(peak)
            worst = slips.index(max(slips))
            out.append(
                Event(
                    t_start=samples[a].t,
                    t_end=samples[b].t,
                    type=EventType.TYRE_ABUSE,
                    confidence=0.6,
                    lap=peak.lap_count,
                    evidence={
                        "打滑轮胎": wheel_names[worst],
                        "该轮滑移率": round(slips[worst], 3),
                        "其他轮滑移率": round(min(slips), 3),
                        # 🔴 胎温 / 悬挂是**真值**（实测两场真实 jsonl 都有数）：
                        #    胎温看哪一侧过热，悬挂看是不是压了路肩/草地
                        #    （一边悬挂被顶起 = 车轮压到路边）。
                        #    tyre_press / tyre_wear 格式 A 恒 0，**不进证据**。
                        # 🔴 胎温保留 1 位小数：四轮差异常常只有零点几度，
                        #    round 成整数会抹成 [60,60,60,60]，看着又像假数据
                        #    （而这个事件的判据恰恰就是"四轮不一样"）。
                        "四轮胎温_C": [round(t, 1) for t in peak.tyre_temp],
                        "四轮悬挂_mm": [round(h * 1000) for h in peak.susp_height],
                    },
                    comment_hint="四轮附着差异过大，可能是单轮压到路肩或草地",
                )
            )
        return out

    # -- 全局分析 ---------------------------------------------------------

    def run_all(self, samples: list[Sample]) -> list[Event]:
        """运行全部检测器，返回按时间排序的事件列表。"""
        events: list[Event] = []
        events += self.detect_impact(samples)
        events += self.detect_spin(samples)
        events += self.detect_hard_braking(samples)
        events += self.detect_offtrack(samples)
        events += self.detect_tyre_abuse(samples)
        events += self.detect_heavy_throttle(samples)

        # 去重：时间重叠度高且类型相同的，只留置信度最高的
        events = self._dedup(events, overlap_thresh=0.5)

        # 按优先级加权排序
        priority = {
            EventType.COLLISION: 10,
            EventType.SPIN: 8,
            EventType.OFF_TRACK: 7,
            EventType.TYRE_ABUSE: 5,
            EventType.HEAVY_THROTTLE: 4,
            EventType.HARD_BRAKING: 3,
        }
        events.sort(key=lambda e: (e.t_start, -priority.get(e.type, 1)))
        return events

    @staticmethod
    def _dedup(events: list[Event], overlap_thresh: float = 0.5) -> list[Event]:
        """按时间重叠去重。"""
        if not events:
            return []
        events = sorted(events, key=lambda e: e.t_start)
        kept: list[Event] = []
        for e in events:
            merged = False
            for k in kept:
                overlap = min(e.t_end, k.t_end) - max(e.t_start, k.t_start)
                shorter = min(e.duration, k.duration) or 1e-9
                if overlap > 0 and overlap / shorter > overlap_thresh and e.type == k.type:
                    if e.confidence > k.confidence:
                        kept[kept.index(k)] = e
                    merged = True
                    break
            if not merged:
                kept.append(e)
        return sorted(kept, key=lambda e: e.t_start)


# ---------------------------------------------------------------------------
# 圈速与复盘分析
# ---------------------------------------------------------------------------

@dataclass
class LapResult:
    lap: int
    lap_time: float
    is_best: bool
    top_speed: float
    min_speed: float
    avg_throttle: float
    avg_brake: float
    avg_slip: float
    max_lat_g: float
    max_long_g: float
    tyre_temp_avg: float
    events: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["lap_time"] = round(self.lap_time, 3)
        return d


def analyze_laps(samples: list[Sample], detector: EventDetector) -> list[LapResult]:
    """逐圈统计分析，标出最快圈。"""
    laps = find_lap_boundaries(samples)
    if not laps:
        return []

    lap_times = [lt for _, _, lt in laps]
    best_idx = min(range(len(laps)), key=lambda i: lap_times[i])

    results = []
    for idx, (a, b, lt) in enumerate(laps):
        seg = samples[a : b + 1]
        if not seg:
            continue
        results.append(
            LapResult(
                lap=seg[0].lap_count,
                lap_time=lt,
                is_best=(idx == best_idx),
                top_speed=max(s.speed_kph for s in seg),
                min_speed=min(s.speed_kph for s in seg),
                avg_throttle=statistics.fmean(s.throttle for s in seg),
                avg_brake=statistics.fmean(s.brake for s in seg),
                avg_slip=statistics.fmean(detector._mean_slip(s) for s in seg),
                max_lat_g=max(lateral_g(s) for s in seg),
                max_long_g=min(longitudinal_g(s) for s in seg),
                tyre_temp_avg=statistics.fmean(
                    statistics.fmean(s.tyre_temp) for s in seg
                ),
                events=len(
                    [
                        e
                        for e in detector.run_all(seg)
                        if e.type
                        in (
                            EventType.COLLISION,
                            EventType.SPIN,
                            EventType.OFF_TRACK,
                            EventType.TYRE_ABUSE,
                        )
                    ]
                ),
            )
        )
    return results


def compare_laps(
    samples: list[Sample], ref_lap: int, cmp_lap: int
) -> dict[str, Any]:
    """
    逐帧对比两圈在相同赛道位置的表现。

    原理：GT7 的 car_x/car_z 是车身坐标（车辆中心/车尾），
    可用来判断是否经过同一个赛道点。对齐后比较车速和油门刹车。

    这是复盘功能的核心——告诉玩家「你在哪一段丢了多少时间」。
    """
    laps = find_lap_boundaries(samples)

    def get_lap(lap_no: int) -> list[Sample] | None:
        for a, b, _ in laps:
            if samples[a].lap_count == lap_no:
                return samples[a : b + 1]
        return None

    ref = get_lap(ref_lap)
    cmp_ = get_lap(cmp_lap)
    if not ref or not cmp_:
        return {"error": f"找不到圈 {ref_lap} 或 {cmp_lap}"}

    # 用车尾坐标 z 做进度归一化：一条圈上 z 从起点到终点单调变化
    # 找出参考圈的最大/最小 z 作为进度范围
    z_min, z_max = min(s.car_z for s in ref), max(s.car_z for s in ref)
    if z_max - z_min < 1e-6:
        return {"error": "赛道坐标无变化，无法归一化"}

    def speed_at_z(seq: list[Sample], target_z: float) -> float | None:
        """在 seq 中找z 最接近 target_z 的采样，返回其速度。"""
        best = min(seq, key=lambda s: abs(s.car_z - target_z))
        return best.speed_kph

    # 在进度 0~100 上采样对比
    progress_points = 50
    deltas = []
    for k in range(progress_points + 1):
        frac = k / progress_points
        target_z = z_min + frac * (z_max - z_min)
        v_ref = speed_at_z(ref, target_z)
        v_cmp = speed_at_z(cmp_, target_z)
        if v_ref is None or v_cmp is None:
            continue
        deltas.append(
            {
                "progress_pct": round(frac * 100, 1),
                "ref_speed": round(v_ref, 1),
                "cmp_speed": round(v_cmp, 1),
                "speed_delta": round(v_cmp - v_ref, 1),
            }
        )

    # 找出差距最大的三个区段（每 10% 为一段）
    losses = []
    for k in range(0, 10):
        window = [d for d in deltas if k * 10 <= d["progress_pct"] < (k + 1) * 10 + 1]
        if window:
            avg_loss = statistics.fmean(w["speed_delta"] for w in window)
            losses.append(
                {
                    "section": f"{k * 10}-{(k + 1) * 10}%",
                    "avg_speed_loss_kph": round(avg_loss, 1),
                    "min_speed_kph": min(w["cmp_speed"] for w in window),
                    "ref_min_speed_kph": min(w["ref_speed"] for w in window),
                }
            )

    losses.sort(key=lambda x: x["avg_speed_loss_kph"])  # 最负的 = 丢时间最多

    return {
        "ref_lap": ref_lap,
        "cmp_lap": cmp_lap,
        "section_losses": losses,
        "curve": deltas,
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def export_report(
    samples: list[Sample],
    detector: EventDetector,
    top_n: int = 20,
) -> dict[str, Any]:
    """生成一份可直接喂给 LLM 的复盘报告。"""
    laps = analyze_laps(samples, detector)
    events = detector.run_all(samples)

    best = next((l for l in laps if l.is_best), None)

    report = {
        "session": {
            "circuit": samples[0].circuit if samples else None,
            "car": samples[0].car_name if samples else None,
            "duration_s": round(samples[-1].t - samples[0].t, 1) if samples else 0,
            "total_samples": len(samples),
            "sample_rate_hz": round(
                len(samples) / (samples[-1].t - samples[0].t), 1
            )
            if len(samples) > 1
            else 0,
        },
        "laps": [l.as_dict() for l in laps],
        "best_lap": best.lap if best else None,
        "events": [
            {
                "t_start": round(e.t_start, 2),
                "t_end": round(e.t_end, 2),
                "type": e.type.value,
                "type_cn": {
                    "collision": "碰撞",
                    "spin": "打滑",
                    "hard_braking": "极限刹车",
                    "off_track": "出界",
                    "overtake": "超车",
                    "heavy_throttle": "大油门",
                    "lap_record": "最快圈",
                    "pit_like": "进站",
                    "tank_slap": "压路肩",
                }.get(e.type.value, e.type.value),
                "confidence": round(e.confidence, 2),
                "lap": e.lap,
                "evidence": e.evidence,
                "hint": e.comment_hint,
            }
            for e in events
        ],
        "event_summary": {},
    }

    # 事件统计汇总
    summary: dict[str, int] = {}
    for e in events:
        summary[e.type.value] = summary.get(e.type.value, 0) + 1
    report["event_summary"] = summary

    # Top 高光时刻（按置信度 × 类型优先级）
    def score(e: Event) -> float:
        w = {
            EventType.COLLISION: 1.5,
            EventType.SPIN: 1.3,
            EventType.OFF_TRACK: 1.2,
            EventType.HARD_BRAKING: 1.0,
            EventType.HEAVY_THROTTLE: 0.6,
        }.get(e.type, 0.8)
        return e.confidence * w

    report["highlights"] = [
        {
            "t_start": round(e.t_start, 2),
            "t_end": round(e.t_end, 2),
            "lap": e.lap,
            "type_cn": {
                "collision": "碰撞",
                "spin": "打滑",
                "hard_braking": "极限刹车",
                "off_track": "出界",
                "heavy_throttle": "大油门",
            }.get(e.type.value, e.type.value),
            "score": round(score(e), 2),
            "evidence": e.evidence,
        }
        for e in sorted(events, key=score, reverse=True)[:top_n]
    ]

    return report


# ---------------------------------------------------------------------------
# 使用示例（造一段假遥测自测；接真实数据时替换load_telemetry）
# ---------------------------------------------------------------------------

def _demo_samples() -> list[Sample]:
    """
    生成一段 60 秒的模拟遥测用于自测，包含：
      - 3 次高速重刹（其中一个是真极限刹车，其余是普通刹车）
      - 1 次失控打滑（四轮同时失去抓地）
      - 1 次出界（低速高滑移）
    模拟数据刻意做了「反例」：普通刹车不该被误报为极限刹车。
    """
    samples: list[Sample] = []
    t = 0.0
    dt = 1 / 60
    lap = 3

    # 重刹事件：t 区间 (起始, 持续, 起始速度, 是否极限)
    brakes = [
        (8.0, 3.0, 250.0, True),    # 极限刹车：从 250 重刹
        (22.0, 4.0, 200.0, False),  # 普通刹车：从 200 常规减速，不该报极限
        (45.0, 2.5, 240.0, True),   # 极限刹车
    ]
    # 失控打滑：(起始时间, 持续时长)
    spin_window = (33.0, 2.0)
    # 出界：(起始时间, 持续时长)
    offtrack_window = (55.0, 3.0)

    while t < 60.0:
        # 失控打滑：(起始, 时长) → 展开成区间
        in_spin = spin_window[0] <= t < spin_window[0] + spin_window[1]
        # 出界：(起始, 时长)
        in_off = offtrack_window[0] <= t < offtrack_window[0] + offtrack_window[1]

        # 速度包络
        speed = 70 + 180 * max(0.0, math.sin(t / 12.0)) ** 0.5
        throttle, brake = 0.9, 0.0
        for b0, dur, bv0, _ in brakes:
            if b0 <= t < b0 + dur:
                brake = 0.95
                throttle = 0.1
                speed = max(60.0, bv0 - (t - b0) * 55.0)
                break

        lat_g = 1.5 if in_spin else 0.55 * math.sin(t / 3.0)
        if in_off:
            speed = 55.0
            slip = 0.35           # 出界时滑移率很高
            brake, throttle = 0.0, 0.3
        elif in_spin:
            slip = 0.22           # 失控时四轮全部打滑
            brake, throttle = 0.2, 0.6
        else:
            slip = 0.02

        # 轮速线速度 = 车速 * (1 + slip)
        base_wheel = (speed / 3.6) / 0.33
        samples.append(
            Sample(
                t=t, seq=int(t * 60), lap_count=lap,
                speed_kph=speed, rpm=2800 + speed * 42,
                gear=max(1, int(speed // 55)), car_x=t, car_z=t % 300.0,
                throttle=throttle, brake=brake, steer_angle=lat_g * 18,
                wheel_speed=[base_wheel * (1 + slip) for _ in range(4)],
                tyre_temp=[84 + (12 if in_spin else 0), 86, 88 + (15 if in_spin else 0), 87],
                tyre_press=[220.0] * 4,
                tyre_wear=[10.0 + t * 0.01] * 4,
                susp_height=[80.0] * 4,
                g_force=[-1.9 if brake > 0.5 else 0.6, lat_g, 0.05],
                circuit="测试赛道", car_name="测试赛车",
            )
        )
        t += dt
    return samples


if __name__ == "__main__":
    print("=" * 60)
    print("GT7 遥测事件检测器 — 自测（使用模拟数据）")
    print("=" * 60)

    samples = _demo_samples()
    detector = EventDetector()
    report = export_report(samples, detector)

    print(f"\n【场次】{report['session']['circuit']} / {report['session']['car']}")
    print(f"  时长 {report['session']['duration_s']}s，采样 {report['session']['total_samples']} 帧"
          f"（{report['session']['sample_rate_hz']} Hz）")

    print(f"\n【圈速】共 {len(report['laps'])} 圈，最快圈 = 第 {report['best_lap']} 圈")
    for l in report["laps"]:
        flag = " ★最快" if l["is_best"] else ""
        print(f"  第{l['lap']}圈  {l['lap_time']:.2f}s  最高{l['top_speed']:.0f}km/h"
              f"  平均G力(横{l['max_lat_g']:.2f}/纵{l['max_long_g']:.2f})"
              f"  事件{l['events']}个{flag}")

    print(f"\n【事件统计】{report['event_summary']}")
    print(f"\n【Top 5 高光时刻】")
    for h in report["highlights"][:5]:
        print(f"  [{h['score']:.2f}] 第{h['lap']}圈 {h['t_start']:.1f}s {h['type_cn']}")
        for k, v in h["evidence"].items():
            print(f"         {k}: {v}")

    print("\n" + "=" * 60)
    print("自测完成。接真实数据时替换 load_telemetry() 即可。")
    print("=" * 60)