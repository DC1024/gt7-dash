# -*- coding: utf-8 -*-
"""进站策略与名次（session_pitstops）—— 回归测试。

合成三圈场次盯死：
  1. gas 环跳 → 一次进站、stint 切成两段、段内均耗正确；
  2. quali_pos 逐帧变化 → 超车（delta<0）/被超（delta>0）事件，
     65535/0 哨兵跳过；
  3. 电车（gas_capacity==0）不做加油检测——进站不加油，环跳判据不成立。
"""
import json

from pathlib import Path


DT = 1.0 / 60.0
N = 600          # 每圈 600 帧 ≈ 10s，速度 100kph → 1980m/圈


def _write_session(path: Path, frames) -> None:
    path.write_text(
        json.dumps({"header": True}) + "\n"
        + "".join(json.dumps(fr) + "\n" for fr in frames),
        encoding="utf-8",
    )


def _frames(gas_profile, pos_profile, cap=100.0):
    """gas_profile: {lap: [(占比0~1, gas), ...] 折线节点（节点间线性）}。
    每圈第一个节点的 gas 即圈首值；pos_profile: {lap: 名次}。"""
    frames = []
    t = 0.0
    for lap in sorted(gas_profile):
        nodes = gas_profile[lap]
        for i in range(N):
            frac = i / (N - 1)
            gas = nodes[-1][1]
            for k in range(len(nodes) - 1):
                a, b = nodes[k], nodes[k + 1]
                if a[0] <= frac <= b[0]:
                    w = (frac - a[0]) / max(1e-9, b[0] - a[0])
                    gas = a[1] + (b[1] - a[1]) * w
                    break
            frames.append({
                "t": t, "lap": lap, "speed_kph": 100.0,
                "gas_level": round(gas, 3), "gas_capacity": cap,
                "quali_pos": pos_profile.get(lap),
            })
            t += DT
    return frames


def test_pitstop_stints_and_position_events(dash, tmp_path):
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, _frames(
        gas_profile={
            1: [(0.0, 100.0), (1.0, 90.0)],
            2: [(0.0, 90.0), (1.0, 80.0)],
            # 半圈时进站：油量递减到 75 后跳回 95（环跳 >5 判进站）
            3: [(0.0, 80.0), (0.5, 75.0), (0.5, 95.0), (1.0, 85.0)],
        },
        pos_profile={1: 5, 2: 4, 3: 6},
    ))
    out = dash.session_pitstops(f)
    assert out["available"] is True
    assert out["powertrain"] == "fuel"

    # —— 进站：1 次，发生在第 3 圈（出站圈），加油 75→95 ——
    assert len(out["pitstops"]) == 1
    p = out["pitstops"][0]
    assert p["lap"] == 3
    assert p["after"] - p["before"] > 15

    # —— stint：2 段。第一段 = 圈 1-2，第二段 = 圈 3 ——
    st = out["stints"]
    assert len(st) == 2
    assert (st[0]["from_lap"], st[0]["to_lap"]) == (1, 2)
    assert (st[1]["from_lap"], st[1]["to_lap"]) == (3, 3)
    assert st[1]["after_stop"] is True and st[0]["after_stop"] is False
    # 段内均耗：第一段 ~20%/2 圈 = 10%/圈
    assert st[0]["fuel_per_lap"] is not None and 5 < st[0]["fuel_per_lap"] < 15

    # —— 名次：超车（5→4，delta=-1）与被超（4→6，delta=+2）——
    evs = [e for e in out["pos_events"] if e.get("from") is not None]
    deltas = [(e["from"], e["pos"], e["delta"]) for e in evs]
    assert (5, 4, -1) in deltas      # 超车
    assert (4, 6, 2) in deltas       # 被超
    # 每圈末名次序列
    assert [(x["lap"], x["pos"]) for x in out["positions"]] == [(1, 5), (2, 4), (3, 6)]


def test_position_sentinels_skipped(dash, tmp_path):
    """65535 / 0 的 quali_pos 是菜单态哨兵，不得当成名次或触发事件。"""
    frames = _frames(
        gas_profile={1: [(0.0, 100.0), (1.0, 90.0)],
                     2: [(0.0, 90.0), (1.0, 80.0)]},
        pos_profile={1: 3, 2: 3},
    )
    # 第 1 圈前 90 帧换成哨兵值
    for i in range(60):
        frames[i]["quali_pos"] = 65535
    for i in range(60, 90):
        frames[i]["quali_pos"] = 0
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, frames)

    out = dash.session_pitstops(f)
    assert out["available"] is True
    # 哨兵帧不产生事件，有效名次只有 3
    assert all(e["pos"] < 65000 and e.get("from", 1) not in (0, 65535)
               for e in out["pos_events"])
    assert out["positions"][-1]["pos"] == 3


def test_ev_session_skips_pit_detection(dash, tmp_path):
    """电车（gas_capacity==0）进站不加油：环跳不得判成进站。"""
    frames = _frames(
        gas_profile={1: [(0.0, 55.0), (1.0, 50.0)],
                     2: [(0.0, 50.0), (1.0, 45.0)],
                     # 电量「回升」（再生回充/换电池语义不明）：不得触发
                     3: [(0.0, 45.0), (0.5, 42.0), (0.5, 80.0), (1.0, 70.0)]},
        pos_profile={1: 2, 2: 2, 3: 2},
        cap=0.0,
    )
    f = tmp_path / "20260101_120000_unknown.jsonl"
    _write_session(f, frames)

    out = dash.session_pitstops(f)
    assert out["available"] is True
    assert out["powertrain"] == "electric"
    assert out["pitstops"] == []
    # 名次照常输出（电车也有名次时间线）
    assert out["positions"][-1]["pos"] == 2
