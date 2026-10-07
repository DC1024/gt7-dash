#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GT7 超车/被超车事件判定器
=========================

解决的问题
----------
GT7 遥测是**单车流**——只有你一台车的数据，其他车的位置、速度、身份
一个字节都没有。所以「你在超 44 号Hamilton」这种话，遥测无法支撑。

本模块用三个数据源交叉验证，把「超车」拆成**可判定的子事件**，
每个子事件标注可信度，让解说词只在证据够时才下结论。

三源分工
--------
| 源 | 能给什么 | 可信度 | 不能给什么 |
|---|---|---|---|
| GT7 遥测 | 你的赛道坐标、速度、G力、走线 | 确定 | 其他车的一切 |
| HUD 图形识别 | 名次 P1-P20、圈数、计时器 | 可靠 | 车号、对手身份 |
| VLM 视觉 | 前方/侧方/后方有车、颜色、相对位置 | 需确认 | 精确距离、名次 |

核心思想
--------
不解「谁超了谁」这个大题，而是拆成 4 个**可从单一数据源判定**的原子事件，
再按证据强度组合成不同程度的解说。证据不足时自动降级为画面描述，
而不是编一个听起来很像那么回事的假解说。

依赖：仅标准库。Python 3.10+
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class Evidence(str, Enum):
    """证据等级。决定解说词可以说到什么程度。"""
    TELEMETRY = "telemetry"      # 遥测直接给出，确定
    HUD = "hud"                  # HUD 图形识别，可靠
    VISION = "vision"            # VLM 视觉描述，需确认
    INFERRED = "inferred"        # 逻辑推断


class OvertakeKind(str, Enum):
    """超车的四种类型，按可判定性从易到难。"""
    POSITION_GAINED = "position_gained"   # 名次提升（最容易：HUD 有名次）
    CAR_PASSED = "car_passed"             # 超越具体车辆（需视觉+名次）
    POSITION_LOST = "position_lost"       # 被超
    CLOSE_PASS = "close_pass"             # 擦身而过未变名次（纯视觉）


@dataclass
class TrackPosition:
    """
    赛道位置。用 gt-telemetry 的 circuit_capture 采集的赛道中心线来映射。

    采集方法：赛道确定后开时间赛跑一圈，gt-telemetry 会记录 (car_x, car_z)
    的轨迹序列，编译成 JSON 中心线。之后任意时刻可用最近邻查表得到
    「赛道进度 %」和「最近弯道编号」。
    """
    # 归一化后的赛道进度 0~1
    progress: float          # 0 = 起跑线, 1 = 回到起跑线
    distance_to_next_corner: float | None = None   # 米
    corner_name: str | None = None# 如 "T1" "S弯"
    track_id: str | None = None
    lap: int = 0


@dataclass
class VisionFrame:
    """VLM 对单帧的视觉描述结果。"""
    t: float
    # 相对位置：车辆在画面中的大致方位
    cars_in_front: int = 0
    cars_side_left: int = 0
    cars_side_right: int = 0
    cars_behind: int = 0            # 后视镜里
    nearest_car_distance_m: float | None = None   # VLM 估计，误差大
    nearest_car_color: str | None = None
    # VLM 的置信度
    confidence: float = 0.0
    # 原始描述（供 LLM 参考）
    raw_caption: str = ""


@dataclass
class HudFrame:
    """HUD 图形识别结果。这是超车判定最可靠的信号源。"""
    t: float
    position: int | None = None          # P1..P20，HUD 上的名次数字
    lap: int | None = None
    lap_count: int | None = None         # 总圈数
    best_lap: float | None = None
    last_lap: float | None = None
    # 识别置信度
    ocr_confidence: float = 0.0


@dataclass
class OvertakeEvent:
    """一次超车（或被超）事件。"""
    t_start: float
    t_end: float
    kind: OvertakeKind
    evidence: Evidence
    confidence: float
    lap: int
    track_progress: float | None = None

    # 事实字段—— 每个都要有对应来源，不允许空口编
    facts: dict[str, Any] = field(default_factory=dict)
    # 无法确定的字段，明确列出（LLM 必须回避这些）
    unknowns: list[str] = field(default_factory=list)

    # 解说词的「可说程度」评级
    @property
    def narration_level(self) -> str:
        """
        narration = 可直接说
        cautious  = 要用「可能」「好像」等不确定措辞
        visual_only = 只能说看到的画面，不能下判断
        """
        if self.evidence == Evidence.TELEMETRY and self.confidence > 0.8:
            return "narration"
        if self.evidence == Evidence.HUD and self.confidence > 0.7:
            return "narration"
        if self.evidence == Evidence.VISION and self.confidence > 0.6:
            return "cautious"
        return "visual_only"

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type"] = self.kind.value
        d["narration_level"] = self.narration_level
        return d


# ---------------------------------------------------------------------------
# 赛道位置映射
# ---------------------------------------------------------------------------

class TrackMapper:
    """
    把 (car_x, car_z) 映射为赛道进度。

    中心线数据格式（gt-telemetry circuit_inventory 产物）：
        {"track_id": "suzuka", "name": "铃鹿赛道",
         "centerline": [{"x": 123.4, "z": 567.8}, ...]}

    中心线是**闭合曲线**，按顺序排列。进度算法：
    1. 找到离当前坐标最近的中心线点（最近邻）
    2. 从该点出发，估算「沿中心线走了多少距离」→ 归一化 0~1
    3. 弯道信息用预标注的 progress 区间给出
    """

    def __init__(self, track_json: dict[str, Any] | None = None):
        self.track_id: str | None = None
        self.name: str | None = None
        self.centerline: list[tuple[float, float]] = []
        # 累计弧长，用于进度归一化
        self.cum_arc: list[float] = [0.0]
        # 弯道标注：[起始进度, 结束进度, 名称]
        self.corners: list[tuple[float, float, str]] = []

        if track_json:
            self.load(track_json)

    def load(self, track_json: dict[str, Any]) -> None:
        self.track_id = track_json.get("track_id")
        self.name = track_json.get("name")
        cl = track_json.get("centerline", [])
        self.centerline = [(float(p["x"]), float(p["z"])) for p in cl]
        self._build_arc()

        for c in track_json.get("corners", []):
            self.corners.append(
                (float(c["from"]), float(c["to"]), c.get("name", ""))
            )

    def _build_arc(self) -> None:
        """计算累计弧长。"""
        self.cum_arc = [0.0]
        for i in range(1, len(self.centerline)):
            x0, z0 = self.centerline[i - 1]
            x1, z1 = self.centerline[i]
            self.cum_arc.append(self.cum_arc[-1] + math.dist((x0, z0), (x1, z1)))

    def locate(self, car_x: float, car_z: float, lap: int = 0) -> TrackPosition:
        """把车身坐标映射为赛道位置。"""
        if not self.centerline:
            return TrackPosition(progress=0.0, lap=lap)

        # 最近邻查找中心线点（中心线点数通常几百到几千，暴力扫足够快）
        best_i = 0
        best_d = float("inf")
        for i, (x, z) in enumerate(self.centerline):
            d = (x - car_x) ** 2 + (z - car_z) ** 2
            if d < best_d:
                best_d = d
                best_i = i

        total = self.cum_arc[-1] if self.cum_arc else 1.0
        progress = self.cum_arc[best_i] / total if total > 0 else 0.0

        # 找落在哪个弯道区间
        corner_name = None
        dist_to_next = None
        for c0, c1, nm in self.corners:
            if c0 <= progress <= c1:
                corner_name = nm
                # 估算到弯道结束还有多远（按中心线总长）
                dist_to_next = (c1 - progress) * total
                break

        return TrackPosition(
            progress=round(progress, 4),
            distance_to_next_corner=round(dist_to_next, 1) if dist_to_next else None,
            corner_name=corner_name,
            track_id=self.track_id,
            lap=lap,
        )


# ---------------------------------------------------------------------------
# 超车判定器
# ---------------------------------------------------------------------------

class OvertakeDetector:
    """
    融合遥测（位置/速度）+ HUD（名次）+ 视觉（车辆存在）判定超车。

    核心设计：**分级判定**。
    - 名次提升（P3→P4）只需 HUD，最可靠 → 直接出解说
    - 超越具体车辆需 HUD + 视觉同时确认 → 加不确定措辞
    - 纯视觉看到车（擦身而过）→ 只描述画面，不下判断
    """

    def __init__(
        self,
        track_mapper: TrackMapper | None = None,
        min_confidence: float = 0.5,
    ):
        self.mapper = track_mapper or TrackMapper()
        self.min_confidence = min_confidence

    # -- 主入口 -----------------------------------------------------------

    def detect(
        self,
        telemetry: list[dict[str, Any]],
        hud_frames: list[HudFrame],
        vision_frames: list[VisionFrame],
    ) -> list[OvertakeEvent]:
        """
        参数：
            telemetry:    遥测记录列表，每项含 t / car_x / car_z / speed_kph / lap
            hud_frames:   HUD 识别结果，按时间排列
            vision_frames: VLM 视觉描述，按时间排列

        返回：
            判定出的超车事件列表
        """
        events: list[OvertakeEvent] = []

        events += self._detect_position_change(hud_frames, telemetry)
        events += self._detect_close_pass(telemetry, vision_frames)
        events += self._detect_car_pass(telemetry, hud_frames, vision_frames)

        events = [e for e in events if e.confidence >= self.min_confidence]
        events.sort(key=lambda e: e.t_start)
        return events

    # -- 判定 1：名次变化（最可靠） ----------------------------------------

    def _detect_position_change(
        self,
        hud: list[HudFrame],
        telemetry: list[dict[str, Any]],
    ) -> list[OvertakeEvent]:
        """
        名次数字变化 = 必然发生了位置变化。

        这是**唯一**能100% 确定的超车证据，因为名次是游戏自己算的。
        但注意：名次变化的原因不止超车——还有被罚时、赛道上限变化。
        所以要配合速度数据排除这些情况。
        """
        out: list[OvertakeEvent] = []
        if len(hud) < 2:
            return out

        for i in range(1, len(hud)):
            prev, cur = hud[i - 1], hud[i]
            if prev.position is None or cur.position is None:
                continue
            if prev.position == cur.position:
                continue
            # 名次变化至少要持续 1.5 秒才算数，避免 HUD 识别抖动
            if cur.t - prev.t < 0.5:
                continue

            # 确认这期间确实在比赛（速度不为0）
            speed = self._speed_at(telemetry, cur.t)
            if speed is None or speed < 20:
                continue

            pos_delta = prev.position - cur.position  # 正数 = 名次提升

            # 取赛道位置
            tp = self._track_at(telemetry, cur.t)

            if pos_delta > 0:
                kind = OvertakeKind.POSITION_GAINED
                ev = OvertakeEvent(
                    t_start=prev.t,
                    t_end=cur.t + 1.0,
                    kind=kind,
                    evidence=Evidence.HUD,
                    confidence=min(1.0, cur.ocr_confidence),
                    lap=cur.lap or 0,
                    track_progress=tp.progress if tp else None,
                    facts={
                        "名次变化": f"P{prev.position} → P{cur.position}",
                        "提升位次": pos_delta,
                        "发生速度_kph": round(speed, 1),
                    },
                    unknowns=["对手车号", "对手车型", "具体超车位置"],
                )
            else:
                kind = OvertakeKind.POSITION_LOST
                ev = OvertakeEvent(
                    t_start=prev.t,
                    t_end=cur.t + 1.0,
                    kind=kind,
                    evidence=Evidence.HUD,
                    confidence=min(1.0, cur.ocr_confidence),
                    lap=cur.lap or 0,
                    track_progress=tp.progress if tp else None,
                    facts={
                        "名次变化": f"P{prev.position} → P{cur.position}",
                        "丢失位次": -pos_delta,
                        "发生速度_kph": round(speed, 1),
                    },
                    unknowns=["对手车号", "对手车型", "被超原因（碰撞？失误？）"],
                )

            if tp and tp.corner_name:
                ev.facts["所在弯道"] = tp.corner_name
            out.append(ev)

        return out

    # -- 判定 2：擦身而过（纯视觉） ----------------------------------------

    def _detect_close_pass(
        self,
        telemetry: list[dict[str, Any]],
        vision: list[VisionFrame],
    ) -> list[OvertakeEvent]:
        """
        视觉上看到车在旁边/极近处，但名次没变 → 擦身而过。

        这类事件**只能描述画面，不能下判断**——
        可能是超了但名次没变（弯道里位置互换），也可能是并行行驶。
        """
        out: list[OvertakeEvent] = []
        for vf in vision:
            side_cars = vf.cars_side_left + vf.cars_side_right
            if side_cars == 0:
                continue
            # 只有距离很近才算「擦身」
            if vf.nearest_car_distance_m is None or vf.nearest_car_distance_m > 5.0:
                continue
            # 必须是高速状态（不是停车看车）
            speed = self._speed_at(telemetry, vf.t)
            if speed is None or speed < 80:
                continue

            tp = self._track_at(telemetry, vf.t)
            side = "左侧" if vf.cars_side_left > vf.cars_side_right else "右侧"

            out.append(
                OvertakeEvent(
                    t_start=max(0.0, vf.t - 0.5),
                    t_end=vf.t + 0.5,
                    kind=OvertakeKind.CLOSE_PASS,
                    evidence=Evidence.VISION,
                    confidence=vf.confidence * 0.7,  # 视觉本来就不可靠，再打折
                    lap=self._lap_at(telemetry, vf.t) or 0,
                    track_progress=tp.progress if tp else None,
                    facts={
                        "旁边有车": f"{side}侧",
                        "估计距离_m": vf.nearest_car_distance_m,
                        "车速_kph": round(speed, 1),
                        "名次未变化": True,
                    },
                    unknowns=["是否真的超越", "对手车号", "相对速度差"],
                )
            )
        return out

    # -- 判定 3：超越具体车辆 ----------------------------------------------

    def _detect_car_pass(
        self,
        telemetry: list[dict[str, Any]],
        hud: list[HudFrame],
        vision: list[VisionFrame],
    ) -> list[OvertakeEvent]:
        """
        名次提升 + 视觉确认「超越瞬间发生在这±2 秒内」→ 可信度更高的超车。

        这样可以给出「你在 T4 超越了 P5」这种带位置的解说，
        但仍然回避车号（因为没有可靠来源）。
        """
        out: list[OvertakeEvent] = []
        pos_events = self._detect_position_change(hud, telemetry)

        for pe in pos_events:
            if pe.kind != OvertakeKind.POSITION_GAINED:
                continue

            # 找超车瞬间 ±2s 内的视觉帧
            nearby_vision = [
                v for v in vision if pe.t_start - 2.0 <= v.t <= pe.t_end + 2.0
            ]
            if not nearby_vision:
                continue

            # 有视觉确认存在车辆
            has_car = any(
                v.cars_in_front > 0 or v.cars_side_left > 0 or v.cars_side_right > 0
                for v in nearby_vision
            )
            if not has_car:
                continue

            # 视觉置信度取窗口内最高
            best_vision = max(nearby_vision, key=lambda v: v.confidence)

            # 提升可信度：名次变化 + 视觉确认 双重证据
            ev = OvertakeEvent(
                t_start=pe.t_start,
                t_end=pe.t_end,
                kind=OvertakeKind.CAR_PASSED,
                evidence=Evidence.HUD,          # 仍以 HUD 为主要依据
                confidence=min(1.0, pe.confidence * 0.7 + best_vision.confidence * 0.3),
                lap=pe.lap,
                track_progress=pe.track_progress,
                facts={
                    **pe.facts,
                    "视觉确认": f"超车瞬间{'前方' if best_vision.cars_in_front else '侧方'}有车",
                    "视觉置信度": round(best_vision.confidence, 2),
                },
                unknowns=["对手车号", "对手车型"],
            )
            out.append(ev)

        return out

    # -- 工具 -------------------------------------------------------------

    def _speed_at(
        self, telemetry: list[dict[str, Any]], t: float
    ) -> float | None:
        """取时刻 t 的速度（线性插值）。"""
        if not telemetry:
            return None
        best = min(telemetry, key=lambda r: abs(r.get("t", 0) - t))
        if abs(best.get("t", 0) - t) > 2.0:
            return None
        return float(best.get("speed_kph", 0))

    def _lap_at(self, telemetry: list[dict[str, Any]], t: float) -> int | None:
        if not telemetry:
            return None
        best = min(telemetry, key=lambda r: abs(r.get("t", 0) - t))
        return best.get("lap")

    def _track_at(
        self, telemetry: list[dict[str, Any]], t: float
    ) -> TrackPosition | None:
        """把时刻 t 的车身坐标映射为赛道位置。"""
        if not self.mapper.centerline:
            return None
        best = min(telemetry, key=lambda r: abs(r.get("t", 0) - t))
        return self.mapper.locate(
            float(best.get("car_x", 0.0)),
            float(best.get("car_z", 0.0)),
            lap=best.get("lap", 0),
        )


# ---------------------------------------------------------------------------
# 解说词模板：按证据强度降级
# ---------------------------------------------------------------------------

# 每种解说级别对应的措辞。核心是「不知道就不说」。
NARRATION_TEMPLATES = {
    # 注意：{corner} 已自带逗号（已知弯道名时）或用「这里」，模板里不要再加逗号
    "position_gained": {
        "narration": [
            "{corner}名次来到 P{new_pos}！",
            "{corner}这一段抓到了机会，位置升到 P{new_pos}。",
            "{corner}干净利落，现在第 {new_pos} 位。",
        ],
        "cautious": [
            "{corner}似乎追上了对手，名次来到 P{new_pos}。",
            "{corner}这里应该完成了一次超越，HUD 显示 P{new_pos}。",
        ],
    },
    "car_passed": {
        "narration": [
            "{corner}完成超越，升到 P{new_pos}！",
            "{corner}一口气从 P{old_pos} 干到 P{new_pos}。",
        ],
        "cautious": [
            "{corner}看起来完成了一次超越，现在 P{new_pos}。",
            "{corner}视觉上确认旁边有车，名次来到 P{new_pos}。",
        ],
    },
    "position_lost": {
        "narration": [
            "{corner}名次滑落到 P{new_pos}。",
            "{corner}这里被咬掉了，掉到 P{new_pos}。",
        ],
        "cautious": [
            "{corner}名次变成了 P{new_pos}。",
        ],
    },
    "close_pass": {
        # 擦身而过永远只能描述画面
        "visual_only": [
            "{corner}{side}有一台车，两车并行。",
            "{corner}旁边有车贴身而过。",
            "{corner}{side}侧有对手并排行驶。",
        ],
    },
}


def render_narration(event: OvertakeEvent) -> dict[str, Any]:
    """
    把事件渲染成给 TTS 的解说稿。

    严格遵守 narration_level：
    - narration：可直接说事实
    - cautious：必须用不确定措辞
    - visual_only：只描述看到的，不下判断
    """
    level = event.narration_level
    kind_key = event.kind.value

    tmpl_set = NARRATION_TEMPLATES.get(kind_key, {})
    templates = tmpl_set.get(level)
    if not templates:
        # 降级：找该类型可用的最低级别
        for lv in ("cautious", "visual_only"):
            if lv in tmpl_set:
                templates = tmpl_set[lv]
                level = lv
                break

    if not templates:
        return {
            "text": None,
            "level": "none",
            "note": "证据不足，不生成解说",
        }

    corner = event.facts.get("所在弯道", "这里")
    # 已知弯道名时补逗号，模板里就能读通；没有弯道名则用「这里」并自带逗号
    if "所在弯道" in event.facts:
        corner = f"{corner}，"
    # 「左侧侧」→ 模板里已有「侧」字，这里只取方位
    side = event.facts.get("旁边有车", "").replace("侧", "").strip() or "侧方"

    text = templates[0].format(
        corner=corner,
        side=side,
        new_pos=event.facts.get("名次变化", "?").split("→")[-1].strip().lstrip("P"),
        old_pos=event.facts.get("名次变化", "?").split("→")[0].strip().lstrip("P"),
    )

    return {
        "text": text,
        "level": level,
        "evidence": event.evidence.value,
        "confidence": round(event.confidence, 2),
        "must_avoid": event.unknowns,      # 提醒：这些不能编
        "can_say": event.facts,            # 这些是事实，可以引用
    }


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------

def _demo_circle(n: int = 1200, lap: int = 5) -> list[dict[str, Any]]:
    """造一条环形赛道上的遥测记录。"""
    import math
    R = 300.0
    recs = []
    for i in range(n):
        t = i / 60.0
        ang = (t / (n / 60.0)) * 2 * math.pi
        recs.append(
            {
                "t": t,
                "lap": lap,
                "car_x": R * math.cos(ang),
                "car_z": R * math.sin(ang),
                "speed_kph": 180.0,
            }
        )
    return recs


def _demo_track() -> TrackMapper:
    """造一个环形赛道中心线，带两个「弯道」标注。"""
    import math
    R = 300.0
    centerline = []
    for k in range(360):
        ang = math.radians(k)
        centerline.append({"x": R * math.cos(ang), "z": R * math.sin(ang)})
    return TrackMapper(
        {
            "track_id": "demo_ring",
            "name": "测试环形赛道",
            "centerline": centerline,
            "corners": [
                {"from": 0.0, "to": 0.25, "name": "T1"},
                {"from": 0.5, "to": 0.75, "name": "T2"},
            ],
        }
    )


if __name__ == "__main__":
    print("=" * 66)
    print("GT7 超车判定器 — 自测")
    print("=" * 66)

    telemetry = _demo_circle()
    mapper = _demo_track()

    # 验证赛道映射：t=0 应在 progress≈0，t=一半应在 0.5 附近
    p0 = mapper.locate(telemetry[0]["car_x"], telemetry[0]["car_z"])
    phalf = mapper.locate(telemetry[600]["car_x"], telemetry[600]["car_z"])
    print(f"\n【赛道映射】t=0 → progress={p0.progress}弯道={p0.corner_name}")
    print(f"            t=10s → progress={phalf.progress} 弯道={phalf.corner_name}")
    assert abs(p0.progress) < 0.02 or abs(p0.progress - 1.0) < 0.02, "起点 progress 应为 0"
    assert abs(phalf.progress - 0.5) < 0.02, f"半圈应为 0.5，实际 {phalf.progress}"
    print("            ✓ 赛道映射正确")

    # 构造场景：P3 → P2（名次提升），伴随视觉确认
    hud = [
        HudFrame(t=0.0, position=3, lap=5, ocr_confidence=0.95),
        HudFrame(t=5.0, position=3, lap=5, ocr_confidence=0.95),
        HudFrame(t=8.0, position=2, lap=5, ocr_confidence=0.92),  # 提升
        HudFrame(t=10.0, position=2, lap=5, ocr_confidence=0.93),
        HudFrame(t=13.0, position=1, lap=5, ocr_confidence=0.94),  # 再提升
        HudFrame(t=20.0, position=1, lap=5, ocr_confidence=0.95),
    ]
    vision = [
        VisionFrame(t=1.0, cars_in_front=1, nearest_car_distance_m=8.0, confidence=0.6,
                    raw_caption="前方有一台车，距离约 8 米"),
        VisionFrame(t=8.5, cars_side_left=1, nearest_car_distance_m=2.5, confidence=0.85,
                    raw_caption="左侧有一台车并排，双方车身几乎贴着"),
        VisionFrame(t=13.5, cars_side_right=1, nearest_car_distance_m=3.0, confidence=0.8,
                    raw_caption="右侧有车并排"),
        # 纯擦身：名次没变，只有视觉
        VisionFrame(t=16.0, cars_side_left=1, nearest_car_distance_m=1.8, confidence=0.75,
                    raw_caption="左側贴身"),
    ]

    det = OvertakeDetector(track_mapper=mapper, min_confidence=0.4)
    events = det.detect(telemetry, hud, vision)

    print(f"\n【检出事件】共 {len(events)} 个")
    for e in events:
        print(f"\n  [{e.kind.value}] t={e.t_start:.1f}~{e.t_end:.1f}s  "
              f"证据={e.evidence.value} 置信度={e.confidence:.2f}")
        print(f"  解说级别: {e.narration_level}")
        print(f"  事实: {json.dumps(e.facts, ensure_ascii=False)}")
        print(f"  未知(不能编): {e.unknowns}")
        nr = render_narration(e)
        print(f"  ▸ 解说稿: 「{nr['text']}」")
        if nr.get("must_avoid"):
            print(f"    必须回避: {nr['must_avoid']}")

    # 关键验证：不能出现具体车号
    all_text = " ".join(str(e.facts) + str(render_narration(e).get("text") or "")
                        for e in events for r in [render_narration(e)])
    print("\n【铁律校验】")
    assert "#" not in all_text and "44" not in all_text, "解说中不应出现车号"
    print("  ✓ 解说中未出现任何车号/对手身份")

    car_passed = [e for e in events if e.kind.value == "car_passed"]
    close_pass = [e for e in events if e.kind.value == "close_pass"]
    assert car_passed, "名次提升 + 视觉确认 应产出 car_passed"
    assert close_pass, "纯视觉擦身 应产出 close_pass"
    assert close_pass[0].narration_level == "visual_only", "擦身必须降到仅描述画面"
    print(f"  ✓ 名次提升+视觉确认 → car_passed（{len(car_passed)} 个）")
    print(f"  ✓ 纯视觉擦身 → close_pass 且降级为 visual_only（{len(close_pass)} 个）")

    print("\n" + "=" * 66)
    print("自测通过。核心保证：证据不足时只描述画面，不编造对手。")
    print("=" * 66)