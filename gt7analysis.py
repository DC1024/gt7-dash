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
          glong(纵向G) / gmag(合成 G 大小) / x / z。dt 异常（<=0 或 >1s）的区间不计距离。
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
            # 合成 G 大小（横向 + 纵向）：与记录器写赛道轨迹点的算法一致，
            # 用来给「按 G 力着色」的赛车线上色。
            "gmag": round(math.hypot(g[0] if len(g) > 0 else 0.0,
                                     g[1] if len(g) > 1 else 0.0), 3),
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

    ref_t_rel_ms = 参考圈跑到该距离用的毫秒数——前端 X 轴同时标注
    「位置(米)」和「参考圈到此处的时间」，用户才知道正负发生在哪。
    """
    if not cur_pts or not ref_pts:
        return {"step": step, "grid": [], "diff_ms": [], "ref_t_rel_ms": []}
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
    ref_ms = [round(_interp(ref_d, ref_t, d) * 1000, 1) for d in grid]
    return {"step": step, "grid": grid, "diff_ms": diff, "ref_t_rel_ms": ref_ms}


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


# —— 踏板渐变赛车线 ————————————————————————————

def _clamp01(v) -> float:
    """把踏板开度规整到 0~1（顺手兼容误传成 0~100 百分数的情况）。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    if x > 1.5:                     # 看起来是百分数（0~100）
        x /= 100.0
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _smooth3(xs: list[float]) -> list[float]:
    """三点滑动平均（首尾取自身），削弱逐点抖动导致的分段碎裂。"""
    n = len(xs)
    if n < 3:
        return xs
    return [(xs[max(0, i - 1)] + xs[i] + xs[min(n - 1, i + 1)]) / 3.0
            for i in range(n)]


def race_line(pts: list[dict], decimate: int = 6, eps: float = 0.04) -> dict:
    """参考圈赛车线：同一批点，**两套着色口径都带出来**。

    · 踏板口径（b / t）：刹车越重越红（粉→红），油门越深越绿（青→绿），都不踩 = 滑行。
    · G 力口径（g）：合成 G 大小（横向+纵向），与实时仪表盘的「按 G 力着色」同一算法。

    返回 {"segments": [{"color": "brake|throttle|coast",
                        "pts": [[x, z], ...],
                        "b": [刹车开度 0~1, ...],   # 与 pts 一一对齐
                        "t": [油门开度 0~1, ...],
                        "g": [合成 G 大小, ...]}]}

    🔴 两个容易踩的坑（都实测踩过）：
    1. **抽稀必须在分段之前对整条序列做**。之前是先分色、再在
       dashboard 里对每段各自 `pts[::6]`——每段末尾到下一个边界的
       点被丢掉，一圈上千个分色段就留下上千个肉眼可见的断隔，
       「明明跑完一整圈，线却是斑驳的」。
    2. **相邻两段必须共享边界点**（新段从上一段的末点接起），
       否则换色处那一帧的弦谁都不画（canvas 分多次 stroke 时
       段与段的衔接靠端点重合，不重合就是缺口）。
    另外对踏板值做三点平滑：逐点抖动会把一条线打成上千个小段。

    ⚠️ 分段是按「踏板通道」切的；切到 G 力着色时前端会把同一条线整条重上色，
       分段本身不影响 G 模式的观感（只是多几次 stroke）。
    """
    sub = pts[::decimate] if decimate and decimate > 1 else pts
    bs = _smooth3([_clamp01(p.get("brake")) for p in sub])
    ts = _smooth3([_clamp01(p.get("throttle")) for p in sub])

    def channel(b: float, t: float) -> str:
        # 两踏板同时踩时以刹车为准（trail braking 视觉上按刹车画）
        if b >= t and b > eps:
            return "brake"
        if t > eps:
            return "throttle"
        return "coast"

    segments: list[dict] = []
    cur_ch, cur_pts, cur_b, cur_t, cur_g = None, [], [], [], []
    for i, p in enumerate(sub):
        c = channel(bs[i], ts[i])
        if c != cur_ch:
            if cur_pts:
                segments.append({"color": cur_ch, "pts": cur_pts,
                                 "b": cur_b, "t": cur_t, "g": cur_g})
                # 从上一段末点接起，保证换色处首尾相接（含各通道值）
                cur_pts = [cur_pts[-1]]
                cur_b, cur_t, cur_g = [cur_b[-1]], [cur_t[-1]], [cur_g[-1]]
            else:
                cur_pts, cur_b, cur_t, cur_g = [], [], [], []
            cur_ch = c
        cur_pts.append([p.get("x"), p.get("z")])
        cur_b.append(round(bs[i], 3))
        cur_t.append(round(ts[i], 3))
        cur_g.append(round(p.get("gmag") or 0.0, 3))
    if cur_pts:
        segments.append({"color": cur_ch, "pts": cur_pts,
                         "b": cur_b, "t": cur_t, "g": cur_g})
    return {"segments": segments}


def race_line_of_lap(frames: list[dict] | None = None, lap_no: int = 0,
                     decimate: int = 6,
                     grouped: dict[int, list[dict]] | None = None) -> dict:
    """指定圈的赛车线（给「行车轨迹」卡片单独切圈用，不必重算整页分析）。

    圈口径复用 clean_laps，与详情页其它分析同一套，避免圈号对不上。

    `grouped` 可选：调用方已有 clean_laps 的结果就直接传进来（`frames` 可不传）。
    切圈是交互动作，每次都拿 21 万帧重跑一遍 clean_laps 太亏。
    """
    laps = clean_laps(frames) if grouped is None else grouped
    fs = laps.get(lap_no)
    if not fs or len(fs) < 2:
        return {"lap": lap_no, "segments": []}
    return {"lap": lap_no, "segments": race_line(lap_samples(fs),
                                                 decimate=decimate)["segments"]}


# —— 分段计时 / 理论最快圈 ——————————————————————

def _lap_sector_split(fs: list[dict], n_sectors: int):
    """一圈按**距离**等分 → (各段用时, 圈长m, 圈总用时s)；采样点不足返回 None。

    用距离等分而不是时间等分：段边界固定在同一段路上，跨圈才有可比性
    （时间等分会让每圈的"段"落在不同位置，段用时差就失去意义）。
    """
    pts = lap_samples(fs)
    if len(pts) < 10:
        return None
    total = pts[-1]["dist"]
    if total <= 0:
        return None
    dists = [q["dist"] for q in pts]
    trels = [q["t_rel"] for q in pts]
    times = []
    for k in range(n_sectors):
        a = _interp(dists, trels, total * k / n_sectors)
        b = _interp(dists, trels, total * (k + 1) / n_sectors)
        times.append(b - a)
    return times, total, pts[-1]["t_rel"]


def sector_times(laps: dict[int, list[dict]], n_sectors: int = 4,
                 tol: float = 0.05, dist_tol: float = 0.03) -> dict:
    """分段计时 + 理论最快圈。

    每圈按距离等分成 n_sectors 段、插值出各段用时；每段取所有**可信圈**里
    的最快值求和 = 理论最快圈。它与实际最快圈的差，就是「你已经有能力跑出来、
    只是还没在同一圈里连起来」的时间 —— 这才是分段计时真正有用的产出。

    🔴 必须先过滤异常圈：冲出赛道 / 进站 / 打转的圈里，某一小段可能
       "恰好很快"，把理论值拉到不真实地低。实测 LM55 23 圈不过滤得到
       +8.48% 的假空间，只留 ≤最快圈*(1+tol) 的 5 圈才是真实的 +0.71%。
       所以理论值**只在可信圈 ≥2 圈时**才给出，否则宁可空着（reliable=False）
       也不给一个会骗人的数字。

    🔴 圈长不一致的圈必须单独排除（dist_tol 那一路）。段边界是按各圈**自己的**
       圈长等分的，所以只有"跑满一整圈"的圈，段边界才落在同一段路上。实测同一场
       里第 1 圈只有 6129.6m（最快圈 6937.8m 的 88%，录像从半圈处开始），
       它的 S1 插出来 25.229s，比全场最快的 33.208s 还"快" 8 秒 —— 纯粹是
       边界错位，不是真本事。
       ⚠️ 这不只是显示难看：**圈长偏短的圈总时长也偏短，完全可能被选成
       actual_best**，那样"实际最快圈"和"潜在空间"一起就错了。所以筛选
       发生在选 actual_best **之前**，不只是从 counted 里剔掉。
       判据用圈长中位数 ±dist_tol（而不是最大值）：出赛道多跑一截会让积分圈长
       变长，取最大反而会以偏为准。

    ⚠️ 理论最快圈 < 实际最快圈是**正常**的，不要把它"修"成相等。
    ⚠️ 残余偏差：即使同为完整圈，圈长仍有约 ±0.3% 的差（本次 5 个可信圈
       6936~6960m），段边界会错开十几米，理论值因此可能乐观 0.1~0.3s。
       所以 laps[].dist_m 要展示出来，让人看得见可比性。
    """
    out: dict = {
        "n_sectors": n_sectors, "tol": tol, "dist_tol": dist_tol,
        "laps": [], "best_each_s": [],
        "theoretical_best_s": None, "actual_best_s": None,
        "actual_best_lap": None,
        "potential_gain_s": None, "potential_gain_pct": None,
        "counted_laps": [], "partial_laps": [], "ref_dist_m": None,
        "reliable": False, "note": "",
    }
    raw: dict[int, tuple] = {}
    for ln, fs in sorted(laps.items()):
        r = _lap_sector_split(fs, n_sectors)
        if r is not None:
            raw[ln] = r
    if not raw:
        out["note"] = "没有可用于分段的圈（采样点不足）"
        return out

    # —— 圈长一致性：只有跑满整圈的圈，段边界才落在同一段路上 ——
    ds = sorted(v[1] for v in raw.values())
    d_ref = ds[len(ds) // 2]
    out["ref_dist_m"] = round(d_ref, 1)
    usable = {n: v for n, v in raw.items()
              if abs(v[1] - d_ref) <= d_ref * dist_tol}
    out["partial_laps"] = sorted(set(raw) - set(usable))
    if not usable:
        # 圈长全都对不上（数据异常），退回全量但明确标注不可信
        usable = dict(raw)
        out["note"] = "所有圈的圈长都偏离中位数，分段不可比"

    actual_best_lap = min(usable, key=lambda n: usable[n][2])
    actual_best = usable[actual_best_lap][2]
    counted = {n: v for n, v in usable.items() if v[2] <= actual_best * (1 + tol)}
    out["actual_best_lap"] = actual_best_lap
    out["actual_best_s"] = round(actual_best, 3)

    if len(counted) >= 2:
        best_each = [min(v[0][k] for v in counted.values())
                     for k in range(n_sectors)]
        theo = sum(best_each)
        out["reliable"] = True
        out["counted_laps"] = sorted(counted)
        out["best_each_s"] = [round(x, 3) for x in best_each]
        out["theoretical_best_s"] = round(theo, 3)
        out["potential_gain_s"] = round(actual_best - theo, 3)
        out["potential_gain_pct"] = round((actual_best - theo) / actual_best * 100, 2)
    else:
        # 只统计了信息性的段最快（不作为结论呈现）
        out["best_each_s"] = [
            round(min(v[0][k] for v in usable.values()), 3) for k in range(n_sectors)]
        out["note"] = (f"接近最快圈（≤{actual_best * (1 + tol):.3f}s）的圈只有 "
                       f"{len(counted)} 个，不足 2 个，理论最快圈不可信")

    for ln in sorted(raw):
        times, dist, total_s = raw[ln]
        partial = ln not in usable
        row = {
            "lap": ln, "total_s": round(total_s, 3), "dist_m": round(dist, 1),
            "sectors": [round(x, 3) for x in times],
            "counted": ln in counted, "is_best": ln == actual_best_lap,
            "partial": partial,
        }
        # 残缺圈的段边界不在同一段路上，给差值只会误导，干脆不给
        if out["reliable"] and not partial:
            be = out["best_each_s"]
            row["deltas"] = [round(times[k] - be[k], 3) for k in range(n_sectors)]
        out["laps"].append(row)
    return out


# —— 轮胎滑移（空转 / 抱死）——————————————————————
#
# 只有 wheel_rads（四轮角速度）是「车轮实际转多快」的来源 —— 车身速度传感器
# 测不出空转和抱死。滑移率 s = (ω·R − v) / v：
#   s > 0 轮子转得比车快（空转 / 打滑）；s < 0 轮子转得比车慢（抱死 / 拖胎）。

# 自由滚动判据（标定只用这些帧）。只有自由滚动时 ω·R ≈ v 才成立。
_SLIP_FREE_THR = 0.05          # 油门开度
_SLIP_FREE_BRK = 0.05          # 刹车开度
_SLIP_FREE_V_MIN = 10.0        # m/s（36 km/h）—— 低速时 ω 与 v 的信噪比都差
_SLIP_FREE_G = 0.3             # 纵向与横向加速度都要小（单位 g）

# 事件阈值。取实测噪声底之上很远：自由滚动帧 |s| ≈ 0.0005，
# 而空转峰值 +16%、抱死最低 −96%，信噪比约 100:1，所以阈值不敏感。
_SLIP_SPIN_THR = 0.10          # 后轴比车快 10% → 空转
_SLIP_LOCK_THR = -0.15         # 前轴比车慢 15% → 抱死
_SLIP_MIN_V = 3.0              # 统计滑移的最小速度（m/s），防低速除出噪声


def _slip_percentiles(xs: list[float]) -> dict:
    if not xs:
        return {}
    s = sorted(xs)
    n = len(s)

    def q(p: float) -> float:
        return round(s[min(n - 1, max(0, int(n * p)))], 3)
    return {"min": round(s[0], 3), "p05": q(0.05), "p50": q(0.50),
            "p95": q(0.95), "max": round(s[-1], 3)}


def wheel_slip(laps: dict[int, list[dict]], nmax_events: int = 5,
               max_per_lap: int = 120) -> dict:
    """用四轮角速度检测空转与抱死。

    🔴 半径是**自标定**的，不依赖任何外部参数：自由滚动帧上 ω·R ≈ v，
       于是 R = Σ(v·ω) / Σ(ω²)（对 R 的最小二乘解，即 argmin Σ(v−ωR)² 的解）。

    🔴 前后轴必须**分别**标定：GT7 前后胎规格常不同，实测 R前 0.3395 m /
       R后 0.3440 m（比值 0.987）。若假设同半径，滑移率会被整体偏置约 1.3%，
       而抱死的均值信号本身只有 −7.8%，偏置不可忽略。

    自洽性检查：标定后自由滚动帧的滑移均值应接近 0。偏得远说明标定被污染
    （自由滚动帧太少，或油门/刹车阈值没生效），此时 calibration.ok 为 False。

    缺 wheel_rads 的场次返回 available=False —— 老场次（含合成的测试数据）
    没有这个字段，前端据此整卡隐藏，不要显示空表。

    抽稀按「每圈目标点数」而不是固定步长：固定步长下长圈点密、短圈点疏，
    而且总输出随帧数线性膨胀（本场 217k 帧、23 圈，步长 4 会产出 54k 点、
    约 2.5 MB JSON）。改成每圈 ≤max_per_lap 点后，输出被圈数封顶
    （23×120 ≈ 2.8k 点 / 约 120 KB），且各圈曲线密度一致、可比。

    ⚠️ 抽稀会漏掉 1~2 帧的尖峰（抱死常常就这么短）。所以图表负责趋势、
       events 负责峰值的分工不能倒过来 —— 尖峰必须从 events 读，别从曲线读。
    """
    per_lap: dict[int, list[tuple]] = {}
    for ln, fs in sorted(laps.items()):
        buf: list[tuple] = []
        for f in fs:
            r = f.get("wheel_rads")
            if not r or len(r) < 4:
                continue
            v = (f.get("speed_kph") or 0.0) / 3.6
            g = f.get("g_force") or (0.0, 0.0, 0.0)
            glon = abs(g[0]) if len(g) > 0 else 0.0
            glat = abs(g[1]) if len(g) > 1 else 0.0
            buf.append((f.get("t") or 0.0, v, (r[0] + r[1]) / 2.0,
                        (r[2] + r[3]) / 2.0, f.get("throttle") or 0.0,
                        f.get("brake") or 0.0, glon, glat))
        if buf:
            per_lap[ln] = buf
    if not per_lap:
        return {"available": False,
                "reason": "本场次没有 wheel_rads（四轮角速度）字段"}

    # —— 1. 自由滚动帧上自标定前后轴半径 ——
    free = [r for rows in per_lap.values() for r in rows
            if r[4] < _SLIP_FREE_THR and r[5] < _SLIP_FREE_BRK
            and r[1] > _SLIP_FREE_V_MIN and r[6] < _SLIP_FREE_G
            and r[7] < _SLIP_FREE_G]
    total_rows = sum(len(v) for v in per_lap.values())

    def _calib(pick) -> float:
        num = sum(r[1] * pick(r) for r in free)
        den = sum(pick(r) ** 2 for r in free)
        return num / den if den > 0 else 0.0

    rf, rr = _calib(lambda r: r[2]), _calib(lambda r: r[3])
    calib: dict = {
        "front_m": round(rf, 4), "rear_m": round(rr, 4),
        "ratio": round(rf / rr, 4) if rr else None,
        "free_frames": len(free), "total_frames": total_rows,
        "free_pct": round(len(free) / total_rows * 100, 1) if total_rows else 0.0,
        "ok": False, "reason": "",
    }
    if len(free) < 50 or rf <= 0 or rr <= 0:
        calib["reason"] = (f"自由滚动帧只有 {len(free)} 帧，标定不可靠"
                          f"（需要 ≥50 帧）")
        return {"available": False, "reason": calib["reason"],
                "calibration": calib}

    def s_front(r) -> float:
        return (r[2] * rf - r[1]) / r[1] if r[1] > 0 else 0.0

    def s_rear(r) -> float:
        return (r[3] * rr - r[1]) / r[1] if r[1] > 0 else 0.0

    # 自洽性：标定用的自由滚动帧上滑移应≈0
    f_mean = sum(s_front(r) for r in free) / len(free)
    r_mean = sum(s_rear(r) for r in free) / len(free)
    calib["free_slip_front"] = round(f_mean, 4)
    calib["free_slip_rear"] = round(r_mean, 4)
    calib["ok"] = abs(f_mean) < 0.02 and abs(r_mean) < 0.02
    if not calib["ok"]:
        calib["reason"] = (f"自由滚动帧滑移均值偏离 0 过多"
                           f"（前 {f_mean:+.4f} / 后 {r_mean:+.4f}），标定可能被污染")

    # —— 2. 逐圈统计 + 事件（连续帧聚成一次，否则一次抱死会被算成 60 次）——
    lap_rows: list[dict] = []
    lock_events: list[dict] = []
    spin_events: list[dict] = []
    series: dict[int, dict] = {}

    def _event(ln: int, t0: float, worst: tuple, getter, nframes: int) -> dict:
        return {
            "lap": ln, "t_rel": round(worst[0] - t0, 3),
            "slip": round(getter(worst), 3),
            "speed_kph": round(worst[1] * 3.6, 1),
            "throttle": round(worst[4] * 100, 0),
            "brake": round(worst[5] * 100, 0),
            "frames": nframes,
        }

    for ln, rows in per_lap.items():
        sf: list[float] = []
        sr: list[float] = []
        lock_run: list[tuple] = []
        spin_run: list[tuple] = []
        dec = {"t": [], "front": [], "rear": [], "speed_kph": [],
               "throttle": [], "brake": []}
        t0 = rows[0][0]
        stride = max(1, math.ceil(len(rows) / max(1, max_per_lap)))

        def _flush_lock(run: list[tuple]) -> None:
            """一次抱死结束：取这一段里**最负**的前轴滑移作为严重度。"""
            if len(run) >= 2:      # 单帧尖峰不算一次事件
                w = min(run, key=s_front)
                lock_events.append(_event(ln, t0, w, s_front, len(run)))

        def _flush_spin(run: list[tuple]) -> None:
            """一次空转结束：取这一段里**最正**的后轴滑移作为严重度。"""
            if len(run) >= 2:
                w = max(run, key=s_rear)
                spin_events.append(_event(ln, t0, w, s_rear, len(run)))

        for i, r in enumerate(rows):
            f_ = s_front(r)
            r_ = s_rear(r)
            if r[1] > _SLIP_MIN_V:
                sf.append(f_)
                sr.append(r_)
                if r[5] > 0.85 and f_ < _SLIP_LOCK_THR:
                    lock_run.append(r)
                else:
                    _flush_lock(lock_run)
                    lock_run = []
                if r[4] > 0.95 and r_ > _SLIP_SPIN_THR:
                    spin_run.append(r)
                else:
                    _flush_spin(spin_run)
                    spin_run = []
            if i % stride == 0:
                dec["t"].append(round(r[0] - t0, 2))
                dec["front"].append(round(f_, 3))
                dec["rear"].append(round(r_, 3))
                dec["speed_kph"].append(round(r[1] * 3.6, 1))
                dec["throttle"].append(round(r[4] * 100, 0))
                dec["brake"].append(round(r[5] * 100, 0))
        _flush_lock(lock_run)
        _flush_spin(spin_run)
        series[ln] = dec

        lock_frames = sum(1 for r in rows if r[1] > _SLIP_MIN_V
                          and r[5] > 0.85 and s_front(r) < _SLIP_LOCK_THR)
        spin_frames = sum(1 for r in rows if r[1] > _SLIP_MIN_V
                          and r[4] > 0.95 and s_rear(r) > _SLIP_SPIN_THR)
        lap_rows.append({
            "lap": ln, "frames": len(rows),
            "front": _slip_percentiles(sf), "rear": _slip_percentiles(sr),
            "lockup_frames": lock_frames, "wheelspin_frames": spin_frames,
        })

    lock_events.sort(key=lambda e: e["slip"])
    spin_events.sort(key=lambda e: e["slip"], reverse=True)
    return {
        "available": True,
        "max_per_lap": max_per_lap,
        "calibration": calib,
        "laps": lap_rows,
        "lockup": {"events": len(lock_events),
                   "frames": sum(r["lockup_frames"] for r in lap_rows),
                   "worst": lock_events[:nmax_events]},
        "wheelspin": {"events": len(spin_events),
                      "frames": sum(r["wheelspin_frames"] for r in lap_rows),
                      "worst": spin_events[:nmax_events]},
        "series": series,
    }


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


def analyze_compare(frames: list[dict] | None = None, ref_lap_no: int | None = None,
                    cur_lap_no: int | None = None, step: float = 10.0,
                    grouped: dict[int, list[dict]] | None = None) -> dict:
    """门面：分组 → 每圈采样 → 选参考圈/对比圈 → 时间差 + 峰谷 + 赛车线。

    `ref_lap_no` 缺省 = 最快圈；`cur_lap_no` 缺省 = 最后一圈（圈号最大的有效圈）。
    两者都允许由调用方（HTTP 查询参数 / UI 下拉）指定，这样用户可以
    任选两圈对比，而不是只能拿最后一圈跟最快圈比。

    `grouped` 可选：调用方若手上已经有 clean_laps 的结果（例如详情页从
    _valid_laps 的记忆化缓存里取），直接传进来即可 —— 此时 `frames` 可以不传。
    详情页一次渲染里 analyze_session 与这里都要同一份分组，不共用就是白扫
    21 万帧两遍。

    clean_laps 会剔除首/末假圈（前圈、完赛离场圈）与菜单态，
    否则 25s 的完赛余圈会被当成「最快圈」画出离场的小段赛车线。
    """
    laps = clean_laps(frames) if grouped is None else grouped
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
        fs = laps.get(n) or []
        # 🔴 duration 必须用原始帧跨度（和 analyze_session 的圈速表同一口径）。
        #    之前用重采样最后一点的 t_rel，终点被采样网格截断，
        #    会出现「表格标第 6 圈最快、选择器默认第 8 圈」的对不上。
        raw_span = (fs[-1]["t"] - fs[0]["t"]) if len(fs) >= 2 else pts[-1]["t_rel"]
        speeds = [p["speed_kph"] for p in pts]
        summary.append({
            "lap": n, "duration_s": round(raw_span, 3),
            "distance_m": pts[-1]["dist"],
            "max_speed_kph": max(speeds),
            "avg_speed_kph": round(sum(speeds) / len(speeds), 1),
        })
    summary.sort(key=lambda s: s["lap"])
    fastest = min(summary, key=lambda s: s["duration_s"])["lap"]
    # 指定的圈号必须真实有效（URL 可能被手改成任意值），
    # 否则回退到默认值，避免 KeyError 把整个详情页打挂。
    if ref_lap_no is None or ref_lap_no not in samples:
        ref_lap_no = fastest
    if cur_lap_no is None or cur_lap_no not in samples:
        cur_lap_no = max(samples)
    # 参考圈与对比圈撞在一起时（用户手选了同一圈，或该场只有一圈被保留），
    # 退到「另一圈」；实在没有别的圈就保持原样（单圈场次时间差自然是全 0）。
    if cur_lap_no == ref_lap_no and len(samples) > 1:
        others = [n for n in sorted(samples) if n != ref_lap_no]
        cur_lap_no = others[-1]
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
