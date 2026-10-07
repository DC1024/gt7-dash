"""圈间对比分析 —— 纯函数库（仅标准库），供仪表盘「对比分析」页与单测使用。

设计约定：
* 输入 frames 为 jsonl 解出的 dict 列表（至少含 t / speed_kph / lap）
* 距离轴：以每帧速度 × Δt 积分（从 0 起累计），是所有对齐的公共坐标
* 时间差定义：diff_ms = t_本圈(d) - t_参考圈(d)，**正值 = 本圈在该距离段丢时间**
"""
from __future__ import annotations

import bisect
import math

# —— 车型解析 ——————————————————————————————

_CAR_CSV_CACHE: dict[str, dict[str, str]] = {}


def car_name_of(code: int, csv_path: str | None = None) -> str:
    """carCode → 车名。表来自 helper/download_cars_csv.py 下载的社区 CSV
    （格式 `ID,ShortName,Maker`，首行表头）。无表或未命中返回 CAR-ID-xxx。

    结果按 csv 路径缓存，多次调用只读一次文件。
    """
    if not code or code <= 0:
        return ""
    key = csv_path or ""
    if key not in _CAR_CSV_CACHE:
        table: dict[str, str] = {}
        if csv_path:
            try:
                with open(csv_path, encoding="utf-8", errors="replace") as fh:
                    for line in fh.read().splitlines()[1:]:
                        seg = line.split(",")
                        if len(seg) >= 2 and seg[0].strip().isdigit():
                            table[seg[0].strip()] = seg[1].strip()
            except OSError:
                pass
        _CAR_CSV_CACHE[key] = table
    return _CAR_CSV_CACHE[key].get(str(code), f"CAR-ID-{code}")


# —— 基础：分组与采样 ——————————————————————————————

def split_laps(frames: list[dict]) -> dict[int, list[dict]]:
    """按圈号分组（保持帧顺序），lap<=0 的帧丢弃。

    ⚠️ 仅按 lap 分组，**不**剔除首/末假圈。需要剔除前圈/末圈
    （开局静止、完赛离场）请用 :func:`clean_laps`。
    """
    laps: dict[int, list[dict]] = {}
    for f in frames:
        lap = f.get("lap") or 0
        if lap <= 0:
            continue
        laps.setdefault(lap, []).append(f)
    return laps


def _lap_peak_kph(frames: list[dict]) -> float:
    """一圈内所有帧的速度峰值 (km/h)。"""
    return max((f.get("speed_kph") or 0.0) for f in frames)


def _lap_distance_m(frames: list[dict]) -> float:
    """一圈内累计行驶距离（米），按速度 × Δt 积分。

    dt 异常（<=0 或 >1s）的区间不计距离，与 lap_samples 一致。
    """
    dist = 0.0
    prev: dict | None = None
    for f in frames:
        if prev is not None:
            dt = (f.get("t") or 0.0) - (prev.get("t") or 0.0)
            if 0.001 < dt < 1.0:
                dist += (prev.get("speed_kph") or 0.0) / 3.6 * dt
        prev = f
    return dist


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if not n:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def clean_laps(frames: list[dict]) -> dict[int, list[dict]]:
    """按圈分组并剔除假圈（保持帧顺序）。

    假圈都源于 GT7 的 lap 字段语义：
      1. 菜单态：lap 为 0xFFFF(65535) 等特殊大值，直接丢弃。
      2. 首圈（比赛开始前，lap=0）与末圈（完赛后离场，末尾 lap=N）：
         这两段**根本没跑完一整条赛道**，是「假圈」。

    🔑 主判据 = **圈距离**（速度积分，单位米）。
       真实圈跑完一整条赛道，各圈距离高度一致（实测同场 5400~5800m）；
       假圈的距离只有几百米（前圈静止/末圈滑行离场）。
       取各圈距离的**中位数** m，圈距离 < 45% × m 判为假圈。
       用中位数而非最大值，低速场次也稳。

    辅助判据 = 峰值速度：某圈几乎全静止（峰值 < 30% × 全场最高峰值）也是假圈。

    ⚠️ 只剔除**首圈与末圈**：中间圈即使跑得慢（事故/慢速圈）也保留，
       避免误删用户的真实数据。单圈场次不剔除（首尾同一圈）。
    """
    laps = split_laps(frames)
    if not laps:
        return laps
    # 菜单态（0xFFFF 等特殊值）视为无效圈
    laps = {n: fs for n, fs in laps.items() if n < 65000}
    if not laps:
        return laps
    ordered = sorted(laps)
    if len(ordered) < 2:                 # 单圈：首尾同一圈，不剔
        return laps

    dists = {n: _lap_distance_m(fs) for n, fs in laps.items()}
    med_d = _median(list(dists.values()))
    peaks = {n: _lap_peak_kph(fs) for n, fs in laps.items()}
    max_p = max(peaks.values())

    first, last = ordered[0], ordered[-1]
    out = dict(laps)
    for n in (first, last):
        # 距离过短 → 没跑完赛道（滑行离场 / 原地静止）
        short = med_d > 0 and dists[n] < 0.45 * med_d
        # 几乎全静止 → 排队 / 停车场
        stalled = max_p > 0 and peaks[n] < 0.30 * max_p
        if short or stalled:
            del out[n]
    return out


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

def match_pv_pairs(peaks_ref: list[dict], peaks_cur: list[dict],
                   ref_total_m: float, cur_total_m: float,
                   tol_frac: float = 0.012) -> list[dict]:
    """把两圈的关键点配成对（同 kind 才配，圈内相对位置差 ≤ tol_frac）。

    🔴 为什么不用绝对距离配对：两圈的速度积分漂移可达上百米
       （实测同场 3/4 圈同弯道相差 60~300m），绝对距离 30m 容差
       只配上 6/17；改用「圈长分数」（相对位置）配 11/17，
       且位置差 <1.1%，弯道对得整整齐齐。

    返回 [{kind, distance, distance_cur, speed_ref, speed_cur, delta}, ...]
    （distance 取参考圈的，delta = 最新圈 − 参考圈，正=最新圈更快）
    """
    pairs: list[dict] = []
    if ref_total_m <= 0 or cur_total_m <= 0:
        return pairs
    taken: set[int] = set()
    for rv in peaks_ref:
        rf = rv["distance"] / ref_total_m
        best: dict | None = None
        best_f = 1e9
        for cv in peaks_cur:
            if cv["kind"] != rv["kind"] or id(cv) in taken:
                continue
            f = abs(cv["distance"] / cur_total_m - rf)
            if f < best_f:
                best_f, best = f, cv
        if best is not None and best_f <= tol_frac:
            taken.add(id(best))
            pairs.append({
                "kind": rv["kind"],
                "distance": rv["distance"],
                "distance_cur": best["distance"],
                "speed_ref": rv["speed_kph"],
                "speed_cur": best["speed_kph"],
                "delta": round(best["speed_kph"] - rv["speed_kph"], 1),
            })
    pairs.sort(key=lambda p: p["distance"])
    return pairs


def analyze_compare(frames: list[dict], ref_lap_no: int | None = None,
                    step: float = 10.0) -> dict:
    """门面：分组 → 每圈采样 → 选参考圈（缺省=最快圈）→ 时间差 + 峰谷 + 赛车线。

    clean_laps 会剔除首/末假圈（前圈、完赛离场圈）与菜单态，
    否则 25s 的完赛余圈会被当成「最快圈」画出离场的小段赛车线。
    """
    laps = clean_laps(frames)
    samples = {n: lap_samples(fs) for n, fs in laps.items()
               if len(fs) >= 30}                     # 少于 30 帧的伪圈跳过
    if not samples:
        return {"laps_analyzed": 0, "lap_summary": [], "ref_lap": None,
                "cur_lap": None, "time_diff": {"step": step, "grid": [],
                                               "diff_ms": []},
                "peaks_ref": [], "peaks_cur": [], "pv_pairs": [],
                "race_line": {"segments": []}}
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
    fastest = min(summary, key=lambda s: s["duration_s"])["lap"]
    # 指定的参考圈必须真实存在（URL 可能被手改成任意值），
    # 否则回退到最快圈，避免 KeyError 把整个详情页打挂。
    if ref_lap_no is None or ref_lap_no not in samples:
        ref_lap_no = fastest
    cur_lap_no = max(samples)
    if cur_lap_no == ref_lap_no and len(samples) > 1:
        cur_lap_no = sorted(samples)[-2]
    r = time_diff(samples.get(cur_lap_no, []), samples[ref_lap_no], step)
    peaks_ref = find_peaks_valleys(samples[ref_lap_no])
    peaks_cur = (find_peaks_valleys(samples[cur_lap_no])
                 if cur_lap_no in samples else [])
    # 圈长：采样点最后一帧的累计距离（m）
    ref_total = samples[ref_lap_no][-1]["dist"]
    cur_total = (samples[cur_lap_no][-1]["dist"]
                 if cur_lap_no in samples else 0.0)
    return {
        "laps_analyzed": len(samples),
        "lap_summary": summary,
        "ref_lap": ref_lap_no,
        "cur_lap": cur_lap_no,
        "time_diff": r,
        "peaks_ref": peaks_ref,
        "peaks_cur": peaks_cur,
        "pv_pairs": match_pv_pairs(peaks_ref, peaks_cur, ref_total, cur_total),
        "race_line": race_line(samples[ref_lap_no]),
    }
