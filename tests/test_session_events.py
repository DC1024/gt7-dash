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
           thr=0.0, brk=0.0, off_m=0.0, tyre_temp=None, susp=None):
    x, z = _circle_xy(i, radius=100.0 + off_m)
    fr = {
        "t": i * DT, "lap": lap, "speed_kph": v_ms * 3.6, "rpm": 5000.0,
        "gear": 3, "throttle": thr, "brake": brk,
        "g_force": [0.0, lat_g, 0.0],
        "car_x": x, "car_z": z,
        "wheel_rads": [kf * v_ms / RF, kf * v_ms / RF,
                       kr * v_ms / RR, kr * v_ms / RR],
    }
    # 胎温 / 悬挂：2026-10-09 起进 _FRAME_COL_KIND，事件证据要读真值
    if tyre_temp is not None:
        fr["tyre_temp"] = list(tyre_temp)
    if susp is not None:
        fr["susp_height"] = list(susp)
    return fr


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


def test_tyre_abuse_evidence_uses_real_tyre_temp(dash, tmp_path):
    """轮胎滥用事件的胎温必须是**真值**（2026-10-09 前恒 0，事件等于哑雷）。

    合成一场：后轴单侧空转（kf/kr 差异）触发 tyre_abuse，帧里带真实胎温。
    """
    f = tmp_path / "20260101_120000_unknown.jsonl"
    frames = []
    for lap in (1, 2):
        base = (lap - 1) * 1500
        for i in range(1500):
            kw = {}
            if lap == 2 and 700 <= i < 760:
                # 后轴整体空转（kr=1.3 → 滑移率 0.3，与干净前轴拉开差值）
                # + 高速，触发 tyre_abuse；帧里带真实胎温/悬挂
                kw = dict(v_ms=30.0, kr=1.3,
                          tyre_temp=[88.0, 92.0, 85.0, 96.0],
                          susp=[0.12, 0.09, 0.20, 0.05])
            else:
                kw = dict(tyre_temp=[70.0, 70.0, 70.0, 70.0],
                          susp=[0.15, 0.15, 0.15, 0.15])
            fr = _frame(base + i, lap, **kw)
            frames.append(fr)
    for fr in frames[1500:]:
        fr["t"] += 1500 * DT
    _write_session(f, frames)

    out = dash.session_events(f)
    assert out["available"] is True
    abuses = [e for e in out["events"] if e["type"] == "tyre_abuse"]
    assert abuses, f"没检出轮胎滥用，现有事件：{[e['type'] for e in out['events']]}"
    ev = abuses[0]["evidence"]
    # 🔴 核心断言：胎温不是四个 0（旧实现占位恒 0，证据是假的）
    assert sum(ev["四轮胎温_C"]) > 0, ev
    assert max(ev["四轮胎温_C"]) >= 90, ev
    assert sum(ev["四轮悬挂_mm"]) > 0, ev


def test_highlights_are_ffmpeg_sliceable(dash, tmp_path):
    """/highlights 输出的切片窗口必须能直接喂 ffmpeg（-ss / -t）。"""
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(spin_at=(720, 750), offtrack_at=(600, 700)))

    hl = dash.session_highlights(f, top=5, pad_before=2.0, pad_after=1.5)
    assert hl["available"] is True
    clips = hl["clips"]
    assert clips, "有事件就该有高光片段"
    # 排序：score 降序，rank 从 1 连续
    assert [c["rank"] for c in clips] == list(range(1, len(clips) + 1))
    assert all(clips[i]["score"] >= clips[i + 1]["score"]
               for i in range(len(clips) - 1))
    for c in clips:
        # 留白：切片起点 = 事件起点 - pad_before（被 0 截断除外）
        if c["t_start"] >= 2.0:
            assert c["clip_start"] == pytest.approx(c["t_start"] - 2.0, abs=1e-6)
        assert c["clip_end"] == pytest.approx(c["t_end"] + 1.5, abs=1e-6)
        assert c["duration"] == pytest.approx(
            c["clip_end"] - c["clip_start"], abs=1e-6)
        assert c["clip_start"] >= 0.0
        assert c["type_cn"]
    # 权重口径：打滑比大油门值钱
    assert dash._HIGHLIGHT_WEIGHT["collision"] > dash._HIGHLIGHT_WEIGHT["hard_braking"]


def test_highlights_params(dash, tmp_path):
    """types 过滤 / top 截断 / min_score 都要生效。"""
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(spin_at=(720, 750), offtrack_at=(600, 700)))

    allc = dash.session_highlights(f, top=0)["clips"]
    assert len(allc) >= 2
    only = dash.session_highlights(f, top=0, types=["off_track"])["clips"]
    assert only and all(c["type"] == "off_track" for c in only)
    top1 = dash.session_highlights(f, top=1)["clips"]
    assert len(top1) == 1
    none_ = dash.session_highlights(f, top=0, min_score=999.0)["clips"]
    assert none_ == []


def test_spin_direction_comes_from_curvature_not_fake_steer(dash, tmp_path):
    """方向必须是算出来的，不是恒 0 的假转向角。

    横向 G 为正 = 左转（两场真实数据用「外侧轮角速度更高」独立验证过），
    合成帧 lat_g=1.3 → 方向「左」，过弯半径 = v²/(a_lat) 量级。
    """
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(spin_at=(720, 750)))
    e = [x for x in dash.session_events(f)["events"] if x["type"] == "spin"][0]
    assert e["evidence"]["方向"] == "左"          # lat_g > 0
    r = e["evidence"]["过弯半径_m"]
    # κ = 1.3×9.80665 / 27.78² ≈ 0.0165 → R ≈ 60m（允许合成数据的粗粒度）
    assert isinstance(r, int) and 30 <= r <= 120
    # 🔴 假数据守门：证据里不许再出现恒 0 的「转向角」
    assert "转向角" not in e["evidence"]


def test_video_anchor_maps_clips_into_video_timeline(dash, tmp_path):
    """绑了录像 → /highlights 每段多给录像时间轴；没绑 → 是 null。

    口径：t_video = t_session − offset_s（offset_s 为负 = 录像比遥测早开始）。
    """
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(spin_at=(720, 750), offtrack_at=(600, 700)))

    # 未绑定：clip_start_video 必须是 null，不能是 0（0 会被当成真值）
    bare = dash.session_highlights(f, top=3, history_dir=tmp_path)
    assert bare["video"]["bound"] is False
    assert all(c["clip_start_video"] is None for c in bare["clips"])

    # 绑定：录像比遥测早开始 18 秒 → offset_s = −18 → 录像时间 = 遥测 + 18
    meta = {f.name: {"video": {"file": "E:/cap/race1.mp4", "offset_s": -18.0,
                               "source": "manual"}}}
    (tmp_path / "sessions_meta.json").write_text(
        __import__("json").dumps(meta), encoding="utf-8")

    info = dash.video_session_info(tmp_path, f)
    assert info["bound"] is True and info["video_lead_s"] == 18.0
    assert info["video_start_epoch"] == pytest.approx(
        info["session_start"] - 18.0, abs=1e-6)

    hl = dash.session_highlights(f, top=3, history_dir=tmp_path)
    assert hl["video"]["bound"] is True
    assert hl["ffmpeg_hint"].count("race1.mp4") == 1
    for c in hl["clips"]:
        assert c["clip_start_video"] == pytest.approx(c["clip_start"] + 18.0, abs=1e-6)
        assert c["clip_end_video"] == pytest.approx(c["clip_end"] + 18.0, abs=1e-6)


def test_spin_direction_comes_from_curvature_not_fake_steer(dash, tmp_path):
    """方向必须是算出来的，不是恒 0 的假转向角。

    横向 G 为正 = 左转（两场真实数据用「外侧轮角速度更高」独立验证过），
    合成帧 lat_g=1.3 → 方向「左」，过弯半径 = v²/(a_lat) 量级。
    """
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(spin_at=(720, 750)))
    e = [x for x in dash.session_events(f)["events"] if x["type"] == "spin"][0]
    assert e["evidence"]["方向"] == "左"          # lat_g > 0
    r = e["evidence"]["过弯半径_m"]
    # κ = 1.3×9.80665 / 27.78² ≈ 0.0165 → R ≈ 60m（允许合成数据的粗粒度）
    assert isinstance(r, int) and 30 <= r <= 120
    # 🔴 假数据守门：证据里不许再出现恒 0 的「转向角」
    assert "转向角" not in e["evidence"]


def test_video_anchor_maps_clips_into_video_timeline(dash, tmp_path):
    """绑了录像 → /highlights 每段多给录像时间轴；没绑 → 是 null。

    口径：t_video = t_session − offset_s（offset_s 为负 = 录像比遥测早开始）。
    """
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _two_lap_frames(spin_at=(720, 750), offtrack_at=(600, 700)))

    # 未绑定：clip_start_video 必须是 null，不能是 0（0 会被当成真值）
    bare = dash.session_highlights(f, top=3, history_dir=tmp_path)
    assert bare["video"]["bound"] is False
    assert all(c["clip_start_video"] is None for c in bare["clips"])

    # 绑定：录像比遥测早开始 18 秒 → offset_s = −18 → 录像时间 = 遥测 + 18
    meta = {f.name: {"video": {"file": "E:/cap/race1.mp4", "offset_s": -18.0,
                               "source": "manual"}}}
    (tmp_path / "sessions_meta.json").write_text(
        __import__("json").dumps(meta), encoding="utf-8")

    info = dash.video_session_info(tmp_path, f)
    assert info["bound"] is True and info["video_lead_s"] == 18.0
    assert info["video_start_epoch"] == pytest.approx(
        info["session_start"] - 18.0, abs=1e-6)

    hl = dash.session_highlights(f, top=3, history_dir=tmp_path)
    assert hl["video"]["bound"] is True
    assert hl["ffmpeg_hint"].count("race1.mp4") == 1
    for c in hl["clips"]:
        assert c["clip_start_video"] == pytest.approx(c["clip_start"] + 18.0, abs=1e-6)
        assert c["clip_end_video"] == pytest.approx(c["clip_end"] + 18.0, abs=1e-6)
