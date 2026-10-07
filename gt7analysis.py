"""圈间对比分析 —— 纯函数库（仅标准库），供仪表盘「对比分析」页与单测使用。

设计约定：
* 输入 frames 为 jsonl 解出的 dict 列表（至少含 t / speed_kph / lap）
* 距离轴：以每帧速度 × Δt 积分（从 0 起累计），是所有对齐的公共坐标
* 时间差定义：diff_ms = t_本圈(d) - t_参考圈(d)，**正值 = 本圈在该距离段丢时间**
"""
from __future__ import annotations

import bisect
import math

# —— 基础：分组与采样 ——————————————————————————————

def split_laps(frames: list[dict]) -> dict[int, list[dict]]:
    """按圈号分组（保持帧顺序），lap<=0 的帧丢弃。"""
    laps: dict[int, list[dict]] = {}
    for f in frames:
        lap = f.get("lap") or 0
        if lap <= 0:
            continue
        laps.setdefault(lap, []).append(f)
    return laps


def lap_samples(frames: list[dict]) -> list[dict]:
    """一圈的帧 → 累计距离采样点。

    每点：dist(米) / t_rel(圈内秒) / v(m/s) / speed_kph / throttle / brake /
          glong(纵向G) / x / z。dt 异常（<=0 或 >1s）的区间不计距离。
    """
    pts: list[dict] = []
    dist = 0.0
    prev = None
    for f in frames:
        t = f.get("t") or 0.0
        v = (f.get("speed_kph") or 0.0) / 3.6
        if prev is not None:
            dt = t - prev["t"]
            if 0.001 < dt < 1.0:
                dist += prev["v"] * dt
        g = f.get("g_force") or [0.0, 0.0, 0.0]
        pts.append({
            "dist": dist, "t": t, "t_rel": 0.0, "v": v,
            "speed_kph": f.get("speed_kph", 0.0),
            "throttle": f.get("throttle", 0.0),
            "brake": f.get("brake", 0.0),
            "glong": g[0] if len(g) > 0 else 0.0,
            "x": f.get("car_x"), "z": f.get("car_z"),
        })
        prev = {"t": t, "v": v}
    if pts:
        t0 = pts[0]["t"]
        for p in pts:
            p["t_rel"] = round(p["t"] - t0, 3)
            p["dist"] = round(p["dist"], 1)
    return pts


# —— 插值与重采样 ——————————————————————————————

def _interp(dists: list[float], values: list[float], d: float) -> float:
    if d <= dists[0]:
        return values[0]
    if d >= dists[-1]:
        return values[-1]
    i = bisect.bisect_right(dists, d) - 1
    d0, d1 = dists[i], dists[i + 1]
    if d1 <= d0:
        return values[i + 1]
    k = (d - d0) / (d1 - d0)
    return values[i] + (values[i + 1] - values[i]) * k


def resample_series(pts: list[dict], key: str, step: float = 10.0,
                    max_dist: float | None = None) -> tuple[list[float], list[float]]:
    """把 pts 里的 (dist, key) 序列重采样到固定距离网格。

    返回 (grid, values)。max_dist 缺省时取该序列的最远距离。
    """
    dists = [p["dist"] for p in pts]
    vals = [p.get(key, 0.0) for p in pts]
    if not dists:
        return [], []
    limit = max_dist if max_dist is not None else dists[-1]
    grid: list[float] = []
    d = 0.0
    while d < limit:
        grid.append(round(d, 1))
        d += step
    return grid, [_interp(dists, vals, d) for d in grid]


# —— 圈间时间差 ——————————————————————————————

def time_diff(cur_pts: list[dict], ref_pts: list[dict],
              step: float = 10.0) -> dict:
    """本圈 vs 参考圈的逐距离时间差。

    diff_ms = t_本圈(d) - t_参考圈(d)。**正值 = 本圈在该距离段丢时间**，
    负值 = 本圈更快。公共距离取两圈较短者。
    """
    if not cur_pts or not ref_pts:
        return {"step": step, "grid": [], "diff_ms": []}
    common = min(cur_pts[-1]["dist"], ref_pts[-1]["dist"])
    grid: list[float] = []
    d = 0.0
    while d < common:
        grid.append(round(d, 1))
        d += step
    cur_d = [p["dist"] for p in cur_pts]
    cur_t = [p["t_rel"] for p in cur_pts]
    ref_d = [p["dist"] for p in ref_pts]
    ref_t = [p["t_rel"] for p in ref_pts]
    diff = [round((_interp(cur_d, cur_t, d) - _interp(ref_d, ref_t, d)) * 1000, 1)
            for d in grid]
    return {"step": step, "grid": grid, "diff_ms": diff}


# —— 峰谷检测 ——————————————————————————————

def _smooth(values: list[float], window: int = 7) -> list[float]:
    if window < 3:
        return list(values)
    half = window // 2
    out = []
    n = len(values)
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + half + 1)
        seg = values[a:b]
        out.append(sum(seg) / len(seg))
    return out


def find_peaks_valleys(pts: list[dict], window: int = 7,
                       prominence_kph: float = 12.0) -> list[dict]:
    """平滑后找速度局部峰/谷，相邻极值差不足 prominence 的丢弃。

    返回 [{"distance": 米, "speed_kph": 值, "kind": "peak"|"valley"}, ...]
    （按距离升序，peak/valley 交替）
    """
    if len(pts) < window * 2:
        return []
    dists = [p["dist"] for p in pts]
    speeds = _smooth([p["speed_kph"] for p in pts], window)
    extrema: list[tuple[int, str]] = []
    for i in range(1, len(speeds) - 1):
        if speeds[i] >= speeds[i - 1] and speeds[i] > speeds[i + 1]:
            kind = "peak"
        elif speeds[i] <= speeds[i - 1] and speeds[i] < speeds[i + 1]:
            kind = "valley"
        else:
            continue
        if extrema and extrema[-1][1] == kind:
            # 同类相邻：保留更极值的一个
            j = extrema[-1][0]
            if (kind == "peak" and speeds[i] >= speeds[j]) or \
               (kind == "valley" and speeds[i] <= speeds[j]):
                extrema[-1] = (i, kind)
            continue
        extrema.append((i, kind))
    # prominence 过滤：相邻极值差 >= 阈值
    out: list[dict] = []
    for idx, (i, kind) in enumerate(extrema):
        if idx > 0:
            j = extrema[idx - 1][0]
            if abs(speeds[i] - speeds[j]) < prominence_kph:
                continue
        out.append({"distance": round(dists[i], 1),
                    "speed_kph": round(speeds[i], 1), "kind": kind})
    # 再次交替过滤
    final: list[dict] = []
    for x in out:
        if final and final[-1]["kind"] == x["kind"]:
            if (x["kind"] == "peak" and x["speed_kph"] > final[-1]["speed_kph"]) or \
               (x["kind"] == "valley" and x["speed_kph"] < final[-1]["speed_kph"]):
                final[-1] = x
            continue
        final.append(x)
    return final


# —— 三色赛车线 ——————————————————————————————

def race_line(pts: list[dict], brake_g: float = -0.25,
              throttle_g: float = 0.12) -> dict:
    """参考圈赛车线：按纵向 G 分色。

    刹车(glong < brake_g) = 红 · 油门(> throttle_g) = 绿 · 其余 = 滑行(蓝)。
    返回 {"segments": [{"color": ..., "pts": [[x, z], ...]}]}
    """
    color_of = lambda g: ("brake" if g < brake_g
                          else "throttle" if g > throttle_g else "coast")
    segments: list[dict] = []
    cur_color, cur_pts = None, []
    for p in pts:
        c = color_of(p["glong"])
        if c != cur_color:
            if cur_pts:
                segments.append({"color": cur_color, "pts": cur_pts})
            cur_color, cur_pts = c, []
        cur_pts.append([p.get("x"), p.get("z")])
    if cur_pts:
        segments.append({"color": cur_color, "pts": cur_pts})
    return {"segments": segments}


# —— 门面 ——————————————————————————————

def analyze_compare(frames: list[dict], ref_lap_no: int | None = None,
                    step: float = 10.0) -> dict:
    """门面：分组 → 每圈采样 → 选参考圈（缺省=最快圈）→ 时间差 + 峰谷 + 赛车线。"""
    laps = split_laps(frames)
    samples = {n: lap_samples(fs) for n, fs in laps.items()
               if len(fs) >= 30}                     # 少于 30 帧的伪圈跳过
    if not samples:
        return {"laps_analyzed": 0, "lap_summary": [], "ref_lap": None,
                "cur_lap": None, "time_diff": {"step": step, "grid": [],
                                               "diff_ms": []},
                "peaks_ref": [], "peaks_cur": [], "race_line": {"segments": []}}
    summary = []
    for n, pts in samples.items():
        speeds = [p["speed_kph"] for p in pts]
        summary.append({
            "lap": n, "duration_s": pts[-1]["t_rel"],
            "distance_m": pts[-1]["dist"],
            "max_speed_kph": max(speeds),
            "avg_speed_kph": round(sum(speeds) / len(speeds), 1),
        })
    summary.sort(key=lambda s: s["lap"])
    if ref_lap_no is None:
        ref_lap_no = min(summary, key=lambda s: s["duration_s"])["lap"]
    cur_lap_no = max(samples)
    if cur_lap_no == ref_lap_no and len(samples) > 1:
        cur_lap_no = sorted(samples)[-2]
    r = time_diff(samples.get(cur_lap_no, []), samples[ref_lap_no], step)
    return {
        "laps_analyzed": len(samples),
        "lap_summary": summary,
        "ref_lap": ref_lap_no,
        "cur_lap": cur_lap_no,
        "time_diff": r,
        "peaks_ref": find_peaks_valleys(samples[ref_lap_no]),
        "peaks_cur": find_peaks_valleys(samples[cur_lap_no]) if cur_lap_no in samples else [],
        "race_line": race_line(samples[ref_lap_no]),
    }
