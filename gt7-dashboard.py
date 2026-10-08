#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GT7 遥测 Web 仪表盘
===================

在接收器旁边跑一个 HTTP 服务，用浏览器看实时遥测。

设计取舍
--------
**不引入任何 Web 框架**（不用 Flask/FastAPI/uvicorn）。
原因：接收器刻意保持「纯标准库」以让镜像只有 ~120MB，
加框架会让镜像涨到 400MB+ 且多一层攻击面。
代价是手写路由和 JSON 序列化，但这个场景只有 3 个接口，完全够用。

数据流
------
    接收器内存缓冲(最新 N 帧)
           ↓ 共享同一进程
    /api/stream   ← 轮询（SSE 在标准库里实现麻烦，轮询足够）
    浏览器 fetch  ← 每 100ms 拉一次
           ↓
    仪表盘页面（原生JS + SVG 画曲线，无第三方图表库）

为什么用轮询而不是 SSE/WebSocket：
    遥测是 60Hz 的一帧帧数据，但仪表盘人眼只需要 10Hz 的刷新。
    轮询实现简单、连接状态好维护、断线自动恢复。
    真要推流，SSE 也就多 20 行——但对这个场景收益不大。

接口
----
    GET /                仪表盘页面（内嵌 HTML/CSS/JS）
    GET /api/state       当前状态 + 最近 N 帧
    GET /api/sessions    历史场次列表
    GET /api/session/<id>  指定场次的统计数据
    GET /api/health      健康状态

用法
----
    python gt7-dashboard.py --port 8787 --history /data
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# 共享状态：接收器写入，仪表盘读取
# ---------------------------------------------------------------------------

class TelemetryHub:
    """
    遥测数据中枢。

    ⚠️ 跨进程读取的设计取舍
    ------------------------
    接收器（gt7-recorder）和仪表盘是两个独立进程，无法共享内存。
    这里采用**状态文件 + 轮询**：接收器每帧用 `os.replace()` 原子替换
    一个 status.json，仪表盘每 100ms 读一次。

    为什么不用其他方案：
      - 共享内存 / 消息队列：需要额外依赖，且 Docker 里跨容器更麻烦
      - Unix Socket：接收器要额外开线程服务连接，动了 UDP 主循环
      - 数据库：为了每秒 10 次写入 overkill，还要引入 sqlite 依赖

    走文件的好处：接收器只多一行 os.replace（原子，不阻塞主循环），
    仪表盘完全无状态，重启就恢复。

    为什么不直接读 jsonl 落盘文件？
      - 落盘文件是追加写的，正在写入的行可能不完整
      - 每 100ms 全量扫一个几十 MB 的文件太浪费
      - 状态文件是「最后一帧」语义，天然适合实时显示
    """

    def __init__(self, status_path: Path, buffer_size: int = 600):
        self._lock = threading.Lock()
        self._buffer: deque[dict[str, Any]] = deque(maxlen=buffer_size)
        self._latest: dict[str, Any] | None = None
        self._session_start: float = 0.0
        self._total_frames = 0
        self._layouts: dict[str, int] = {}
        self._warning: str | None = None
        self._connected = False
        self._last_frame_t = 0.0
        # 本场发车位（接收器在开跑瞬间快照，见 gt7-recorder）
        self._grid_start = 0
        self._has_coords = False
        self._session_max_speed = 0.0
        # 赛道轨迹 [[x,z,G], ...] 与 G-G 散点 [[横向,纵向], ...]
        self._path: list = []
        self._gg: list = []
        self._lap_times: list = []
        self._lap_fuel: list = []
        # 本场动力类型（fuel/electric/kart）与能量回收峰值（kW 量级）
        self._powertrain: str = "fuel"
        self._max_energy_recovery: float = 0.0
        self._status_path = status_path
        self._last_mtime = 0.0

    def refresh(self) -> None:
        """
        读状态文件，把新帧并入缓冲。

        由 HTTP handler 每次请求时调用——没有后台线程，
        因为没人访问时没必要读文件。

        ⚠️ 历史数据从状态文件里的紧凑数组整批拿，而不是自己累加。
            遥测 60Hz、轮询 10Hz，只靠轮询累积会丢掉 5/6 的点
            （早期版本就是这样，曲线只有 1 帧）。
        """
        try:
            if not self._status_path.exists():
                with self._lock:
                    self._connected = False
                return

            payload = json.loads(self._status_path.read_text(encoding="utf-8"))
            frame = payload.get("frame")
            if not frame:
                return

            # 接收器给的紧凑历史（字段缩写）→ 展开成前端要的格式
            history = [
                {
                    "t": h["t"],
                    "speed_kph": h["s"],
                    "rpm": h["r"],
                    "gear": h["g"],
                    "throttle": h["th"],
                    "brake": h["bk"],
                    "lap": h["lap"],
                    "tyre_temp": h["wt"],
                    "wheel_rads": h["wf"],
                    "g_force": h["gf"],
                }
                for h in payload.get("history", [])
            ]

            with self._lock:
                # 整批覆盖（而不是 append）——状态文件里已是完整历史
                if history:
                    self._buffer.clear()
                    self._buffer.extend(history)
                self._latest = frame
                self._session_start = payload.get("session_start", 0.0)
                self._total_frames = payload.get("frames_total", self._total_frames)
                self._layouts = payload.get("layouts", {})
                self._warning = payload.get("warning")
                self._has_coords = bool(payload.get("has_coords"))
                self._session_max_speed = payload.get("session_max_speed", 0.0)
                # 轨迹与 G-G 散点是**降频**发的（接收器侧 2Hz），
                # 字段缺失时保留上一次的值，避免地图一闪一闪。
                if "path" in payload:
                    self._path = payload["path"]
                if "gg" in payload:
                    self._gg = payload["gg"]
                if "lap_times" in payload:
                    self._lap_times = payload["lap_times"]
                if "lap_fuel" in payload:
                    self._lap_fuel = payload["lap_fuel"]
                if "powertrain" in payload:
                    self._powertrain = payload["powertrain"]
                if "max_energy_recovery" in payload:
                    self._max_energy_recovery = payload["max_energy_recovery"]
                self._recording = bool(payload.get("recording"))
                self._source_ips = payload.get("source_ips", [])
                self._ps5_filter = payload.get("ps5_filter", "auto")
                self._grid_start = int(payload.get("grid_start") or 0)
                self._last_frame_t = time.time()
                # 🔴 connected 不能只看「状态文件存在且能解析」：
                #    PS5 关机后接收器不再更新文件，但旧文件还在，
                #    仪表盘会永远显示「采集中」。用文件写入时刻判新鲜度：
                #    超过 5 秒没更新（接收器 20Hz 写）= 数据流已断。
                stale = time.time() - float(payload.get("t") or 0.0) > 5.0
                self._connected = not stale

        except (OSError, json.JSONDecodeError, ValueError, KeyError):
            with self._lock:
                self._connected = False

    def _car_name_of(self, latest: dict | None) -> str:
        """carCode → 车名。表来自 helper/download_cars_csv.py 下载的社区 CSV。"""
        if not latest:
            return ""
        code = latest.get("car_code") or 0
        if code <= 0:
            return ""
        import gt7analysis
        return gt7analysis.car_name_of(
            code, str(self._status_path.parent / "cars.csv")
        )

    def snapshot(self, max_frames: int = 300) -> dict[str, Any]:
        """仪表盘页面拉取。"""
        self.refresh()
        with self._lock:
            frames = list(self._buffer)[-max_frames:]
            latest = dict(self._latest) if self._latest else None
            lap_time = 0.0
            if latest and self._session_start:
                lap_time = max(0.0, latest["t"] - self._session_start)

            return {
                "connected": self._connected,
                "grid_start": self._grid_start,
                "frames": self._total_frames,
                "layouts": dict(self._layouts),
                "warning": self._warning,
                "has_coords": self._has_coords,
                "session_max_speed": round(self._session_max_speed, 1),
                "path": self._path,
                "gg": self._gg,
                "lap_times": self._lap_times,
                "lap_fuel": self._lap_fuel,
                "powertrain": self._powertrain,
                "max_energy_recovery": self._max_energy_recovery,
                "car_name": self._car_name_of(latest),
                "session_duration": round(time.time() - self._session_start, 1)
                if self._session_start
                else 0,
                "lap_time": round(lap_time, 3),
                "latest": latest,
                "history": frames,
                "server_time": time.time(),
            }

    def stats(self) -> dict[str, Any]:
        """全局统计。"""
        self.refresh()
        with self._lock:
            if not self._buffer:
                return {
                    "frames": 0, "connected": False, "layouts": {},
                    "has_coords": self._has_coords,
                }
            buf = list(self._buffer)
            speeds = [f["speed_kph"] for f in buf]
            rpms = [f["rpm"] for f in buf]
            return {
                "connected": self._connected,
                "frames": self._total_frames,
                "buffered": len(buf),
                "layouts": dict(self._layouts),
                "max_speed": round(max(speeds), 1),
                "max_rpm": round(max(rpms), 0),
                "avg_speed": round(sum(speeds) / len(speeds), 1),
                "has_coords": self._has_coords,
            }


# 全局单例——main() 里按实际路径注入
HUB: TelemetryHub


# ---------------------------------------------------------------------------
# 历史场次
# ---------------------------------------------------------------------------

# 场次 jsonl 解析缓存：path -> (mtime, size, header, frames)。
# 🔴 /session 一次请求会跑 analyze_session + compare_session 两个分析，
#    各自把 30MB jsonl 读一遍纯属浪费；切换参考圈(?ref_lap=N)更是
#    反复重读同一文件。缓存解析结果后，二次请求只算不读，
#    页面响应从秒级降到亚秒级。jsonl 落盘后不可变，mtime+size 失效足够。
_FRAMES_CACHE: dict[str, tuple[float, int, dict, list]] = {}


def _load_frames(path: Path) -> tuple[dict, list]:
    """读场次 jsonl（header + 全部帧），带 2 条目缓存。"""
    key = str(path)
    try:
        st = path.stat()
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return {}, []
    hit = _FRAMES_CACHE.get(key)
    if hit and (hit[0], hit[1]) == sig:
        return hit[2], hit[3]
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
    except OSError:
        return {}, []
    if len(lines) < 2:
        return {}, []
    header = json.loads(lines[0])
    frames = [json.loads(x) for x in lines[1:] if x.strip()]
    while len(_FRAMES_CACHE) >= 2:      # 只留最近 2 个场次，防内存膨胀
        _FRAMES_CACHE.pop(next(iter(_FRAMES_CACHE)))
    _FRAMES_CACHE[key] = (sig[0], sig[1], header, frames)
    return header, frames


def compare_session(path: Path, ref_lap_no: int | None = None) -> dict[str, Any]:
    """读场次 jsonl → 圈间对比分析（gt7analysis 纯函数库）。

    `ref_lap_no` 可选：指定参考圈号（赛车线 / 对比基准）。
    缺省由 gt7analysis 取最快圈。

    失败永远返回 {"error": ...} 而不是抛出——对比是增值功能，
    不能因为它挂掉影响详情页主体。
    """
    try:
        _, all_frames = _load_frames(path)
        frames = [f for f in all_frames if "lap" in f]
        import gt7analysis
        # 赛车线抽稀在 gt7analysis.race_line 内部做（decimate=6）。
        # 🔴 不能在这里对每段各自 [::6]：分段后各自抽稀会把每段末尾
        #    到下一个边界的点丢掉，一圈上千个分色段留下上千个断隔。
        r = gt7analysis.analyze_compare(frames, ref_lap_no=ref_lap_no)
        return r
    except Exception as e:
        return {"error": str(e)}



# 场次元数据（收藏 / 自定义名称）存 data/sessions_meta.json：
# jsonl 是不可变的原始数据，用户的标注必须放在边车文件里，
# 删除/改名都只动这个文件（删除例外：jsonl 移入 _trash 可找回）。
def load_settings(history_dir: Path) -> dict[str, Any]:
    """全局设置（data/settings.json）。"""
    fp = history_dir / "settings.json"
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(history_dir: Path, settings: dict[str, Any]) -> None:
    fp = history_dir / "settings.json"
    fp.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")


def load_session_meta(history_dir: Path) -> dict[str, Any]:
    fp = history_dir / "sessions_meta.json"
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_session_meta(history_dir: Path, meta: dict[str, Any]) -> None:
    fp = history_dir / "sessions_meta.json"
    fp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def _first_car_code(path: Path) -> int:
    """轻量读场次首帧的 car_code（车型码，用于列表展示）。

    只读文件头几行，避免扫描整个 jsonl。首帧可能在菜单态
    （car_code 或为 0），此处尽力而为，没读到返回 0。
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.readline()                     # 跳过 header
            for _ in range(40):               # 最多看 40 帧
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                f = json.loads(line)
                c = f.get("car_code") or 0
                if c > 0:
                    return c
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return 0


# 场次最快圈缓存：列表页每次刷新都要问每个文件的最快圈，
# 整包解析太重，这里只流式扫 (lap, t) 两个字段；文件没变直接命中。
_BEST_LAP_CACHE: dict[tuple[float, int], float | None] = {}


def _best_lap_of(path: Path) -> float | None:
    """场次的最快有效圈（秒，圈跨度 ≥20s 才算，口径同 analyze_session）。"""
    try:
        st = path.stat()
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    if key in _BEST_LAP_CACHE:
        return _BEST_LAP_CACHE[key]
    best: float | None = None
    cur: int | None = None
    t0 = t_prev = 0.0

    def _close() -> None:
        nonlocal best
        if cur is not None and t_prev - t0 >= 20.0 \
                and (best is None or t_prev - t0 < best):
            best = round(t_prev - t0, 3)

    try:
        with path.open("r", encoding="utf-8") as fh:
            fh.readline()                       # header 行
            for line in fh:
                try:
                    f = json.loads(line)
                except json.JSONDecodeError:
                    continue
                lap = f.get("lap")
                t = f.get("t") or 0.0
                if not isinstance(lap, int) or lap <= 0 or lap >= 65000:
                    continue
                if lap != cur:                  # 换圈 = 上一圈结束
                    _close()
                    cur, t0 = lap, t
                t_prev = t
        _close()
    except OSError:
        return None
    _BEST_LAP_CACHE[key] = best
    return best


def _is_anomalous(best) -> bool:
    """异常场次：没有任何完成圈（best=None），或唯一圈 <20s（本应用口径 <20s 不算有效圈）。"""
    return best is None or (isinstance(best, (int, float)) and best < 20)


def _recording_active(history_dir: Path) -> bool:
    """借状态文件判断此刻是否正在录制，避免在 list_sessions 时把进行中的比赛误移进回收站。"""
    st = history_dir / "status.json"
    if not st.is_file():
        return False
    try:
        p = json.loads(st.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not p.get("recording"):
        return False
    ts = p.get("t")
    if not isinstance(ts, (int, float)):
        return False
    # 状态文件 20Hz 更新；10s 内无更新说明数据流已断（车关机/断开）
    return (time.time() - float(ts)) < 10.0


def _move_session_to_trash(f: Path, history_dir: Path) -> bool:
    """把单场 jsonl 移入 _trash，成功返回 True（回收站已有同名 / 失败返回 False）。"""
    trash = history_dir / "_trash"
    dest = trash / f.name
    if dest.exists():
        return False
    try:
        trash.mkdir(exist_ok=True)
        f.rename(dest)
        return True
    except OSError:
        return False


DEFAULT_NAME_TEMPLATE = "{车型} {时间} {最快圈}"


def _fmt_lap_name(sec: float | None) -> str:
    if not sec:
        return "-"
    m = int(sec // 60)
    return f"{m}:{sec - m * 60:06.3f}"


def _session_display_name(s: dict, tpl: str) -> str:
    """按模板生成默认显示名。手动改名（custom_name）优先，由调用方处理。

    支持占位符（中英皆可）：{车型}/{car} {时间}/{time} {最快圈}/{lap}
    {场次}/{circuit}。模板里出现未知占位符时回退文件名，不让页面挂掉。
    """
    name = tpl or DEFAULT_NAME_TEMPLATE
    for zh, en in (("{车型}", "{car}"), ("{时间}", "{time}"),
                   ("{最快圈}", "{lap}"), ("{场次}", "{circuit}")):
        name = name.replace(zh, en)
    try:
        out = name.format(
            car=s.get("car_name") or "未知车",
            time=s.get("time_str") or s.get("modified", ""),
            lap=_fmt_lap_name(s.get("best_lap_s")),
            circuit=s.get("circuit") or "",
        ).strip()
    except (KeyError, IndexError, ValueError):
        out = ""
    return out or s.get("circuit") or s.get("file", "")


def list_sessions(history_dir: Path, limit: int = 30) -> list[dict[str, Any]]:
    """列出已落盘的场次文件（合并收藏/自定义名称，收藏优先展示）。"""
    if not history_dir.exists():
        return []

    meta = load_session_meta(history_dir)
    csv_path = str(history_dir / "cars.csv")
    tpl = load_settings(history_dir).get(
        "session_name_template", DEFAULT_NAME_TEMPLATE)
    sessions = []
    # 是否正在录制（一次判定，循环内复用）——用于保护活场不被自动归档
    live = _recording_active(history_dir)
    now = time.time()
    for f in sorted(history_dir.glob("*.jsonl"), reverse=True)[:limit]:
        try:
            stat = f.stat()
            parts = f.stem.split("_", 2)
            m = meta.get(f.name) or {}
            import gt7analysis
            car_name = gt7analysis.car_name_of(_first_car_code(f), csv_path)
            # 文件名里的时间戳 → 短格式「10-08 00:27」
            ts = (parts[0] or "") if parts else ""
            tod = (parts[1] if len(parts) > 1 else "")[:6]
            time_str = (f"{ts[4:6]}-{ts[6:8]} {tod[:2]}:{tod[2:4]}"
                        if len(ts) >= 8 and len(tod) >= 4 else "")
            best = _best_lap_of(f)
            anomalous = _is_anomalous(best)
            # 异常场次（菜单/停车场/刚点火残片：无完成圈或唯一圈 <20s）直接归档到
            # 回收站，可恢复；但「正在录制且文件近 20s 内被写入」的活场不挪动，
            # 否则会把刚开跑、还没跑完一圈的比赛误判并移位。
            if anomalous and not (live and (now - stat.st_mtime) < 20.0):
                if _move_session_to_trash(f, history_dir):
                    continue
            entry = {
                "file": f.name,
                "timestamp": parts[0] if parts else "",
                "time_of_day": parts[1] if len(parts) > 1 else "",
                "circuit": parts[2] if len(parts) > 2 else "unknown",
                "size_kb": round(stat.st_size / 1024, 1),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "car_name": car_name,
                "best_lap_s": best,
                "anomalous": anomalous,
                "time_str": time_str,
                # —— 用户标注 ——
                "favorite": bool(m.get("favorite")),
                "custom_name": m.get("custom_name") or "",
            }
            # 展示名：手动改名优先，否则按默认模板组合
            entry["display_name"] = (entry["custom_name"]
                                     or _session_display_name(entry, tpl))
            sessions.append(entry)
        except OSError:
            continue
    # 收藏的排前面，其余按时间倒序（原顺序）
    sessions.sort(key=lambda s: (not s["favorite"],))
    return sessions


def _dominant_car_code(frames: list[dict]) -> int:
    """全场出现次数最多的非 0 car_code（一场通常同一辆车）。"""
    from collections import Counter
    cnt: Counter[int] = Counter()
    for f in frames:
        c = f.get("car_code") or 0
        if c > 0:
            cnt[c] += 1
    return cnt.most_common(1)[0][0] if cnt else 0


def analyze_session(path: Path) -> dict[str, Any]:
    """读取一个已落盘场次做离线统计。"""
    try:
        header, frames = _load_frames(path)
        if not frames:
            return {"error": "文件为空"}

        speeds = [f["speed_kph"] for f in frames]
        rpms = [f["rpm"] for f in frames]

        # —— 车型：取全场 car_code 的非 0 众数 → 车名 ——
        # （不同车对圈速影响极大，场次里必须能看出开的是什么车）
        car_code = _dominant_car_code(frames)
        car_name = ""
        if car_code:
            import gt7analysis
            car_name = gt7analysis.car_name_of(car_code, str(path.parent / "cars.csv"))

        # —— 按圈分组（clean_laps 自动剔除前圈 / 完赛离场圈 / 菜单态）——
        import gt7analysis
        laps = gt7analysis.clean_laps(frames)

        lap_times = []
        for lap_no, fs in sorted(laps.items()):
            if len(fs) < 2:
                continue
            # 用帧数×间隔估算更稳：只取首尾两帧的跨度，
            # 中间即使丢包也不影响总时长。
            duration = fs[-1]["t"] - fs[0]["t"]
            # ⚠️ 一圈至少要 20 秒（GT7 最短赛道也1分多）。
            #    数据不足 20 秒说明这是「采集刚开始」或「刚进圈」，
            #    此时报出来的圈速是假的，宁可不给。
            if duration < 20.0:
                lap_times.append(
                    {"lap": lap_no, "time": round(duration, 3), "incomplete": True}
                )
                continue
            lap_times.append({"lap": lap_no, "time": round(duration, 3)})

        valid_laps = [l for l in lap_times if not l.get("incomplete")]

        return {
            "header": header,
            "frame_count": len(frames),
            "duration": round(frames[-1]["t"] - frames[0]["t"], 1),
            "max_speed": round(max(speeds), 1),
            "avg_speed": round(sum(speeds) / len(speeds), 1),
            "max_rpm": round(max(rpms), 0),
            "laps": lap_times,
            "best_lap": min(valid_laps, key=lambda x: x["time"]) if valid_laps else None,
            "has_coords": any(f.get("has_coords") for f in frames),
            "car_code": car_code,
            "car_name": car_name,
            "layouts": {
                k: sum(1 for f in frames if f.get("layout") == k)
                for k in ("A", "B", "C")
                if any(f.get("layout") == k for f in frames)
            },
        }
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"error": f"解析失败: {e}"}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

PAGE = ""  # 由 build_page() 填充


# ---------------------------------------------------------------------------
# 公开 API v1 —— 稳定接口，给第三方程序读取解密后的遥测
# ---------------------------------------------------------------------------

def _v1_live(snap: dict[str, Any]) -> dict[str, Any]:
    """把内部 snapshot 映射成对外稳定结构。

    🔴 v1 的字段名与单位是**对外承诺**：只能新增，不能改名/改单位。
       内部怎么重构都行，改这个适配函数即可。
    """
    L = snap.get("latest") or {}
    g = L.get("g_force") or [0.0, 0.0, 0.0]
    gl = g[0] if len(g) > 0 else 0.0
    gt_ = g[1] if len(g) > 1 else 0.0
    history = [
        {
            "t": f.get("t"), "speed_kph": f.get("speed_kph"), "rpm": f.get("rpm"),
            "gear": f.get("gear"), "throttle": f.get("throttle"),
            "brake": f.get("brake"), "lap": f.get("lap"),
            "tyre_temp_c": f.get("tyre_temp"), "g_force": f.get("g_force"),
        }
        for f in snap.get("history", [])
    ]
    lts = snap.get("lap_times") or []
    return {
        "meta": {
            "api_version": 1,
            "server_time": time.time(),
            "source": "GT7 UDP telemetry (Salsa20 decrypted locally)",
        },
        "connected": snap.get("connected", False),
        "session": {
            "frames": snap.get("frames", 0),
            "duration_s": snap.get("session_duration", 0),
            "max_speed_kph": snap.get("session_max_speed", 0.0),
            "completed_laps": lts[-1][0] if lts else 0,
            "lap_times": lts,           # [[第几圈, 毫秒], ...]
            "lap_fuel": snap.get("lap_fuel", []),   # [[第几圈, 油耗%], ...]
        },
        "car": {
            "speed_kph": L.get("speed_kph", 0.0),
            "rpm": L.get("rpm", 0.0),
            "car_code": L.get("car_code", 0),
            "car_name": snap.get("car_name", ""),
            "gear": L.get("gear", 0),
            "suggested_gear": L.get("suggested_gear", 0),
            "throttle": L.get("throttle", 0.0),          # 0~1
            "brake": L.get("brake", 0.0),                # 0~1
            "position_m": {"x": L.get("car_x", 0.0),
                            "y": L.get("car_y", 0.0),
                            "z": L.get("car_z", 0.0)},
            "velocity_ms": L.get("velocity", [0.0, 0.0, 0.0]),
            "g_force": {"longitudinal": gl, "lateral": gt_,
                        "magnitude": round(math.hypot(gl, gt_), 3)},
            "fuel_pct": L.get("gas_level", 0.0),
            "fuel_capacity_l": L.get("gas_capacity", 0.0),
            # —— 动力类型与能量（新增，稳定承诺）——
            # powertrain: fuel / electric / kart。electric 时 fuel_pct 是**剩余电量 kWh**，
            #            fuel_capacity_l 为 0。energy_recovery 仅在扩展包(~)时有值，
            #            默认格式 A 恒为 0（不代表没有回收，而是协议没给）。
            "powertrain": snap.get("powertrain", "fuel"),
            "energy_recovery": L.get("energy_recovery", 0.0),
            "max_energy_recovery": snap.get("max_energy_recovery", 0.0),
            "throttle_filtered": L.get("throttle_filtered", 0.0),
            "brake_filtered": L.get("brake_filtered", 0.0),
            "turbo_boost": L.get("turbo_boost", 0.0),
            "engine": {
                "oil_pressure_bar": L.get("oil_pressure", 0.0),
                "water_temp_c": L.get("water_temp", 0.0),
                "oil_temp_c": L.get("oil_temp", 0.0),
                "body_height_m": L.get("body_height", 0.0),
            },
            "shift_alert": {"min_rpm": L.get("min_alert_rpm", 0.0),
                            "max_rpm": L.get("max_alert_rpm", 0.0),
                            "shift_now": bool(
                                L.get("max_alert_rpm", 0.0) > 0
                                and L.get("rpm", 0.0) >= L.get("max_alert_rpm", 0.0))},
            "race": {"time_of_day_ms": L.get("time_of_day", 0),
                     # 🔴 0x84 在比赛中是「当前名次」（随排名实时变），
                     #    真正的发车位见 grid_start（开跑瞬间快照）
                     "grid_position": L.get("quali_pos", 0),
                     "grid_start": snap.get("grid_start", 0),
                     "num_cars": L.get("num_cars", 0)},
            "tyre_temp_c": L.get("tyre_temp", []),
            "suspension_height_m": L.get("susp_height", []),
            "wheel_rev_per_s": L.get("wheel_rads", []),
            "flags": L.get("flags", 0),
            "state": {"on_track": L.get("car_on_track", False),
                      "paused": L.get("paused", False),
                      "loading": L.get("loading", False)},
        },
        "timing": {
            "current_lap": L.get("lap", 0),
            "laps_in_race": L.get("laps_in_race", 0),
            "best_lap_ms": L.get("best_lap_ms"),
            "last_lap_ms": L.get("last_lap_ms"),
            "current_lap_time_s": snap.get("lap_time", 0.0),
        },
        "track": {"path": snap.get("path", []),
                  "gg_samples": snap.get("gg", [])},
        "history": history,
    }


API_DOCS_MD = """# GT7 遥测公开 API v1

给第三方程序读取**已解密、已解析**的 GT7 遥测数据。
所有接口均为 GET，返回 UTF-8 JSON；全部带 CORS 头，浏览器端可直接调用。

- Base URL: `http://localhost:8787`（局域网内其他机器用 `http://<服务器IP>:8787`）
- 数据源：GT7 官方 UDP 遥测（33740 端口，Salsa20 本地解密），60 帧/秒
- 本文档也可通过 `GET /api/v1/docs` 获取（text/markdown）

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/live?frames=N` | 实时遥测：最新一帧 + 最近 N 帧历史 + 轨迹 + G-G 散点 |
| GET | `/api/v1/laps` | 当前场次的每圈成绩与最快圈 |
| GET | `/api/v1/sessions` | 历史场次文件列表 |
| GET | `/api/v1/sessions/<文件名>` | 指定场次的统计摘要 |
| GET | `/api/v1/sessions/<文件名>/download` | 下载原始 jsonl（attachment） |
| POST | `/api/v1/sessions/<文件名>/favorite` | 收藏/取消收藏，body `{"value": true}` |
| POST | `/api/v1/sessions/<文件名>/rename` | 改名，body `{"value": "新名称"}` |
| POST | `/api/v1/sessions/<文件名>/delete` | 删除（移入服务器 `data/_trash/`，保留期后自动清除） |
| GET | `/api/v1/settings` | 回收站保留期/清单与场次命名模板 |
| POST | `/api/v1/settings/trash-retention` | 设置回收站保留天数，body `{"value": 30}`（0=永不清理） |
| POST | `/api/v1/settings/name-template` | 场次默认命名模板，body `{"value": "{车型} {时间} {最快圈}"}` |
| POST | `/api/v1/trash/purge` | 清空回收站（彻底删除全部） |
| POST | `/api/v1/trash/<文件名>/restore` | 从回收站恢复场次到列表 |
| POST | `/api/v1/trash/<文件名>/delete` | 彻底删除回收站中的单个场次 |
| GET | `/api/v1/docs` | 本文档 |

`favorite` 与 `custom_name` 会合并在 `GET /api/v1/sessions` 的返回里
（`favorite: bool`、`custom_name: string`），收藏的场次排在最前。
列表每项还带 `car_name`（车型短名，从 `cars.csv` 查 ShortName，查不到为空串）。
`anomalous: bool` 表示异常场次（没有任何完成圈 / 唯一圈 <20s，多为菜单、停车场、
刚点火的残片）。这类场次在「历史场次」页加载时会**自动归档到下方回收站**（可恢复，
保留期内可一键恢复），因此主列表里通常看不到它们；回收站卡片内可勾选「只看异常场次」
单独筛出这些异常项。进行中的活场不会被误移（靠 status.json 的录制状态保护）。

### 参数

- `frames=N`：历史帧数，live 默认 120、上限 600；sessions 详情默认 0（不带回帧）
- `ref_lap=N`（sessions 详情）：指定参考圈号做赛道线/时间差对比分析，
  默认取最快圈；圈号不存在（如手改 URL）自动回退最快圈。
- 返回 404 的情形：场次文件名不存在 / 非法路径

## 字段与单位约定（对外承诺，只加不改）

### 顶层
| 字段 | 类型 | 说明 |
|---|---|---|
| `meta.api_version` | int | 接口版本，当前 1 |
| `meta.server_time` | float | 服务器 Unix 时间戳（秒） |
| `connected` | bool | 是否正在收到 PS5 遥测 |

### `session`
| 字段 | 类型 | 说明 |
|---|---|---|
| `frames` | int | 本场已落盘帧数 |
| `duration_s` | float | 本场时长（秒） |
| `max_speed_kph` | float | 本场极速（km/h） |
| `completed_laps` | int | 已完成圈数 |
| `lap_times` | array | `[[第几圈, 圈速毫秒], ...]` |
| `car_code` | int | 车型码（全场非 0 众数；读不到为 0） |
| `car_name` | string | 车型短名，`cars.csv` 查 ShortName，查不到空串 |
| `laps` | array | 逐圈详情，见下表 |
| `best_lap` | object 或 null | 最快圈 `{lap, time}`；无完整圈为 null |

`laps[]` 每项：
| 字段 | 类型 | 说明 |
|---|---|---|
| `lap` | int | 圈号 |
| `time` | float | 圈速（秒） |
| `incomplete` | bool | true = 该圈数据不足 20s，判为不完整（不计入 best_lap） |

> 圈速/赛道线分析统一走 `clean_laps` 剔除假圈：前圈（开局排队静止）、
> 末圈（完赛滑行离场）、菜单态（lap=65535），以圈距离偏离中位数判假。
> 列表与详情里的圈速表都不再出现这些假圈。

### `car`（速度/温度/开度等的单位都标在字段名里）
| 字段 | 单位 | 说明 |
|---|---|---|
| `speed_kph` | km/h | 车速 |
| `rpm` | rpm | 发动机转速 |
| `gear` / `suggested_gear` | int | 当前档 / 建议档，0 = 空挡 |
| `throttle` / `brake` | 0~1 | 踏板开度（乘 100 即百分比） |
| `position_m` | 米 | 车辆世界坐标 x/y/z（y 为高度） |
| `velocity_ms` | m/s | 速度矢量（模长 ≈ speed_kph/3.6） |
| `g_force.longitudinal` | g | 纵向：正值加速、负值刹车 |
| `g_force.lateral` | g | 横向：**正值右转、负值左转** |
| `g_force.magnitude` | g | 合力大小 |
| `fuel_pct` | 0~100 | 剩余油量百分比；**纯电车时是剩余电量 kWh**（看 `powertrain`） |
| `fuel_capacity_l` | 升 | 油箱容量（纯电为 0） |
| `powertrain` | 枚举 | 动力类型：`fuel`（燃油/混动）/ `electric`（纯电）/ `kart` |
| `energy_recovery` | kW 量级 | 能量回收功率；**仅扩展包(~)有值**，默认格式 A 恒为 0 |
| `max_energy_recovery` | kW 量级 | 本场能量回收峰值 |
| `throttle_filtered` | 0~1 | 游戏滤波后的油门输出（仅扩展包） |
| `brake_filtered` | 0~1 | 游戏滤波后的刹车输出（仅扩展包） |
| `turbo_boost` | bar 级 | 涡轮压力 |
| `engine` | 对象 | 引擎健康：`oil_pressure_bar` / `water_temp_c` / `oil_temp_c` / `body_height_m` |
| `shift_alert` | 对象 | 换挡提示：`min_rpm` / `max_rpm` / `shift_now`（转速已达换挡点） |
| `race` | 对象 | 比赛信息：`time_of_day_ms`（赛道时钟）/ `grid_position`（**当前名次**，0x84 在比赛中随排名实时变）/ `grid_start`（发车位，开跑瞬间快照；0=未捕获）/ `num_cars`（参赛车数） |
| `tyre_temp_c` | ℃ | 四轮表面温度，顺序 FL/FR/RL/RR |
| `suspension_height_m` | 米 | 四轮悬挂行程，顺序 FL/FR/RL/RR |
| `wheel_rev_per_s` | 转/秒 | 四轮转速（带符号，倒挡为负） |
| `flags` | bit 位 | bit0 在赛道 / bit1 暂停 / bit2 加载 / bit3 在挡 … |
| `state.on_track` | bool | 是否在赛道上（比赛进行中） |

### `timing`
| 字段 | 单位 | 说明 |
|---|---|---|
| `current_lap` | int | 当前圈号 |
| `laps_in_race` | int | 本局总圈数 |
| `best_lap_ms` / `last_lap_ms` | 毫秒 | 最快圈 / 上一圈（null = 还没跑完） |
| `current_lap_time_s` | 秒 | 本圈已用时 |

### `track`
| 字段 | 说明 |
|---|---|
| `path` | `[[x, z, 该点G值], ...]` 约 10Hz 采样的整车轨迹（画行车轨迹用） |
| `gg_samples` | `[[横向g, 纵向g], ...]` 约 16Hz 采样的 G-G 散点 |

### `history[]`（每帧一条）
`t`（服务器时间戳秒）、`speed_kph`、`rpm`、`gear`、`throttle`、`brake`、
`lap`、`tyre_temp_c`、`g_force`。

## 使用示例

```bash
# 实时数据（最近 60 帧）
curl "http://localhost:8787/api/v1/live?frames=60"

# 只要圈速
curl "http://localhost:8787/api/v1/laps"

# 历史场次列表，然后取某场的统计
curl "http://localhost:8787/api/v1/sessions"
curl "http://localhost:8787/api/v1/sessions/20261007_045628_unknown_6ac5607c.jsonl"

# 详情页分析（赛车线 / 时间差对比）；?ref_lap=N 指定参考圈（缺省=最快圈）
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl"
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl&ref_lap=3"
```

## 稳定性说明

- v1 字段**只加不改名不改单位**；将来不兼容的改动会升到 v2 并保留 v1
- `history` 的条数上限 600；`path`/`gg` 上限 4000/1200 点
- 服务器单线程 HTTP，请勿高频轮询（≥100ms 间隔为宜）
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "GT7Dashboard/1.0"

    # 历史场次目录。由 main() 挂在「服务器实例」上，
    # ⚠️ 不是挂在类上——handler 里读的是 self.server.history_dir，
    #    设到 Handler 类上是无效的（踩过：设类上 → 属性不存在报错）。
    # 用类变量做默认值兜底，main() 会覆盖 server 实例上的值。
    history_dir: str = "./data"

    # -- 工具 -------------------------------------------------------------

    def _send_json(self, obj: Any, code: int = 200, cors: bool = False) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:      # 公开 API v1 带跨域头，第三方网页可直接调用
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, code: int = 200, cors: bool = False) -> None:
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        """CORS 预检。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _send_html(self, html: str, code: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- 场次管理（收藏 / 改名 / 删除）------------------------------------
    # jsonl 是不可变原始数据：收藏与改名写进 sessions_meta.json 边车文件，
    # 删除则把 jsonl 移入 data/_trash/（可人工找回，不是真删）。

    def do_POST(self) -> None:
        # 🔴 整个方法必须兜异常：未捕获的异常发生在响应发送前，
        #    客户端会收到空响应（HTTP 000），且看不出任何错误信息。
        try:
            return self._do_post_impl()
        except Exception as e:
            try:
                self._send_json({"error": "internal error",
                                 "detail": str(e)}, 500, cors=True)
            except Exception:
                pass

    def _do_post_impl(self) -> None:
        parsed = urlparse(self.path)
        # —— 全局设置：POST /api/v1/settings/trash-retention {"value": 天数} ——
        if parsed.path == "/api/v1/settings/trash-retention":
            hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                body = {}
            try:
                v = float(body.get("value"))
            except (TypeError, ValueError):
                self._send_json({"error": "value 必须是数字（天数，0=永不清理）"},
                                400, cors=True)
                return
            if not (0 <= v <= 3650):
                self._send_json({"error": "天数需在 0~3650 之间"}, 400, cors=True)
                return
            s = load_settings(hist)
            s["trash_retention_days"] = v
            save_settings(hist, s)
            self._send_json({"ok": True, "trash_retention_days": v,
                             "hint": "已生效，下次清理（每小时检查一次）按新保留期执行"},
                            cors=True)
            return

        # —— 默认名称模板：POST /api/v1/settings/name-template {"value": "..."} ——
        if parsed.path == "/api/v1/settings/name-template":
            hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                body = {}
            v = str(body.get("value") or "").strip()[:120]
            if not v:
                v = DEFAULT_NAME_TEMPLATE      # 存空 = 回到默认模板
            s = load_settings(hist)
            s["session_name_template"] = v
            save_settings(hist, s)
            self._send_json({"ok": True, "session_name_template": v,
                             "hint": "已保存，列表页刷新后生效"}, cors=True)
            return

        # —— 回收站操作：POST /api/v1/trash/purge | /api/v1/trash/<name>/<action> ——
        tseg = parsed.path.split("/")
        if len(tseg) >= 4 and tseg[1:4] == ["api", "v1", "trash"]:
            hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
            trash = hist / "_trash"
            if len(tseg) == 5 and tseg[4] == "purge":
                if not trash.is_dir():
                    self._send_json({"ok": True, "purged": 0}, cors=True)
                    return
                cnt = 0
                for tf in trash.glob("*.jsonl"):
                    try:
                        tf.unlink()
                        cnt += 1
                    except OSError:
                        continue
                self._send_json({"ok": True, "purged": cnt,
                                 "hint": f"已彻底删除 {cnt} 个文件"}, cors=True)
                return
            if len(tseg) != 6 or not tseg[5]:
                self._send_json({"error": "not found"}, 404, cors=True)
                return
            name, action = Path(tseg[4]).name, tseg[5]
            target = (trash / name).resolve()
            if (not str(target).startswith(str(trash)) or not target.exists()
                    or target.suffix != ".jsonl"):
                self._send_json({"error": "回收站里没有这个文件", "file": name},
                                404, cors=True)
                return
            if action == "restore":
                dest = hist / name
                if dest.exists():
                    self._send_json({"error": "同名场次已存在，无法恢复"},
                                    409, cors=True)
                    return
                target.rename(dest)
                self._send_json({"ok": True, "hint": "已恢复到场次列表"}, cors=True)
            elif action == "delete":
                target.unlink()
                self._send_json({"ok": True, "hint": "已彻底删除"}, cors=True)
            else:
                self._send_json({"error": "unknown action"}, 404, cors=True)
            return

        seg = parsed.path.split("/")
        # /api/v1/sessions/<name>/<action>
        if (len(seg) != 6 or seg[1:4] != ["api", "v1", "sessions"]
                or not seg[5]):
            self._send_json({"error": "not found"}, 404, cors=True)
            return
        name, action = Path(seg[4]).name, seg[5]
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}

        hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
        target = (hist / name).resolve()
        if (not str(target).startswith(str(hist)) or not target.exists()
                or target.suffix != ".jsonl"):
            self._send_json({"error": "session not found", "file": name},
                            404, cors=True)
            return

        meta = load_session_meta(hist)
        entry = meta.setdefault(name, {})

        if action == "rename":
            new = str(body.get("value") or "").strip()[:60]
            if not new:
                self._send_json({"error": "名称不能为空"}, 400, cors=True)
                return
            entry["custom_name"] = new
        elif action == "favorite":
            entry["favorite"] = bool(body.get("value"))
        elif action == "delete":
            trash = hist / "_trash"
            trash.mkdir(exist_ok=True)
            target.rename(trash / name)      # 移入回收目录，可找回
            meta.pop(name, None)
            save_session_meta(hist, meta)
            self._send_json({"ok": True, "deleted": name,
                             "hint": "文件已移入 data/_trash/，可人工找回"},
                            cors=True)
            return
        else:
            self._send_json({"error": "unknown action", "action": action},
                            400, cors=True)
            return

        save_session_meta(hist, meta)
        self._send_json({"ok": True, "action": action, "file": name}, cors=True)

    # -- 关闭默认日志（每100ms 一次请求会刷屏）----------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    # -- 路由 -------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        try:
            if path == "/":
                self._send_html(PAGE)

            elif path == "/api/state":
                # ?frames=300 控制返回的帧数
                n = int(query.get("frames", ["300"])[0])
                self._send_json(HUB.snapshot(max_frames=max(10, min(n, 600))))

            elif path == "/api/stats":
                self._send_json(HUB.stats())

            elif path == "/api/sessions":
                hist = Path(self.server.history_dir)  # type: ignore[attr-defined]
                self._send_json(
                    {"sessions": list_sessions(hist), "data_dir": str(hist)}
                )

            elif path == "/sessions":
                # 历史场次页面（给用户看的，不是裸 JSON）
                hist = Path(self.server.history_dir)  # type: ignore[attr-defined]
                self._send_html(build_sessions_page(hist))

            elif path == "/session":
                # 单场次详情页
                name = query.get("file", [""])[0]
                if not name:
                    self._send_html("<h1>缺少 file 参数</h1>", 400)
                    return
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                # 目录穿越防护
                if not str(target).startswith(str(hist)) or not target.exists():
                    self._send_html("<h1>文件不存在或非法路径</h1>", 404)
                    return
                # 参考圈选择器：?ref_lap=N 指定赛车线看第几圈，缺省=最快圈。
                # 🔴 「最快圈」必须用 analyze_session 的口径（真实帧跨度、
                #    剔除 <20s 的残圈），而不是 analyze_compare 内部拿重采样
                #    duration 再取 min——两者分母不同，会出现「表格标第 6 圈
                #    最快、选择器却默认第 8 圈」的对不上。这里显式传 best_lap。
                ref_raw = query.get("ref_lap", [""])[0]
                stats = analyze_session(target)
                try:
                    ref_lap_no = int(ref_raw) if ref_raw else None
                except ValueError:
                    ref_lap_no = None
                if ref_lap_no is None:
                    ref_lap_no = (stats.get("best_lap") or {}).get("lap")
                self._send_html(build_session_page(
                    target, stats, ref_lap_no=ref_lap_no))

            elif path == "/api/session":
                name = query.get("file", [""])[0]
                if not name:
                    self._send_json({"error": "缺少 file 参数"}, 400)
                    return
                # 防目录穿越
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                if not str(target).startswith(str(hist)) or not target.exists():
                    self._send_json({"error": "文件不存在或非法路径"}, 404)
                    return
                self._send_json(analyze_session(target))

            elif path == "/api/health":
                s = HUB.stats()
                self._send_json(
                    {
                        "ok": True,
                        "connected": s.get("connected", False),
                        "frames": s.get("frames", 0),
                        "has_coords": s.get("has_coords", False),
                    }
                )

            # ---- 公开 API v1（稳定接口，带 CORS，供第三方程序读取）----
            elif path == "/api/v1/live":
                try:
                    n = int(query.get("frames", ["120"])[0])
                except ValueError:
                    n = 120
                self._send_json(_v1_live(HUB.snapshot(max_frames=max(1, min(n, 600)))),
                                cors=True)

            elif path == "/api/v1/laps":
                snap = HUB.snapshot(max_frames=1)
                lts = snap.get("lap_times") or []
                self._send_json({
                    "meta": {"api_version": 1, "server_time": time.time()},
                    "connected": snap.get("connected", False),
                    "completed_laps": lts[-1][0] if lts else 0,
                    "best_lap_ms": min((ms for _, ms in lts), default=None),
                    "lap_times": lts,
                }, cors=True)

            elif path == "/api/v1/sessions":
                hist = Path(self.server.history_dir)  # type: ignore[attr-defined]
                self._send_json({"meta": {"api_version": 1},
                                 "data_dir": str(hist),
                                 "sessions": list_sessions(hist)}, cors=True)

            elif path.startswith("/api/v1/sessions/") and path.endswith("/download"):
                # /api/v1/sessions/<名>/download —— 流式下发原始 jsonl
                seg = path.split("/")
                name = Path(seg[4]).name if len(seg) == 6 else ""
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                if (not str(target).startswith(str(hist)) or not target.exists()
                        or target.suffix != ".jsonl"):
                    self._send_json({"error": "session not found", "file": name},
                                    404, cors=True)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(target.stat().st_size))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(target, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return

            elif path.startswith("/api/v1/sessions/"):
                # /api/v1/sessions/<文件名> —— 防目录穿越：只取文件名部分
                name = Path(path.rsplit("/", 1)[-1]).name
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                if (not str(target).startswith(str(hist)) or not target.exists()
                        or target.suffix != ".jsonl"):
                    self._send_json({"error": "session not found", "name": name},
                                    404, cors=True)
                    return
                data = {"meta": {"api_version": 1, "file": name},
                        **analyze_session(target)}
                self._send_json(data, cors=True)

            elif path == "/api/v1/settings":
                hist = Path(self.server.history_dir)  # type: ignore[attr-defined]
                s = load_settings(hist)
                trash = hist / "_trash"
                tfiles = list(trash.glob("*.jsonl")) if trash.is_dir() else []
                tsize = 0
                try:
                    tsize = sum(f.stat().st_size for f in tfiles)
                except OSError:
                    pass
                trash_items = []
                for tf in sorted(tfiles, key=lambda x: x.name, reverse=True):
                    try:
                        st = tf.stat()
                        tb = _best_lap_of(tf)
                        trash_items.append({
                            "name": tf.name,
                            "size_kb": round(st.st_size / 1024, 1),
                            "modified": datetime.fromtimestamp(
                                st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "anomalous": _is_anomalous(tb),
                        })
                    except OSError:
                        continue
                self._send_json({
                    "meta": {"api_version": 1},
                    "trash_retention_days": s.get("trash_retention_days", 30),
                    "trash_files": len(tfiles),
                    "trash_size_mb": round(tsize / 1048576, 1),
                    "trash": trash_items,
                    "session_name_template": s.get(
                        "session_name_template", DEFAULT_NAME_TEMPLATE),
                }, cors=True)

            elif path == "/api/v1/docs":
                self._send_text(API_DOCS_MD, cors=True)

            else:
                self._send_json({"error": "not found"}, 404)

        except BrokenPipeError:
            # 浏览器刷新页面时会断连接，忽略
            pass
        except Exception as e:
            self._send_json({"error": str(e)}, 500)


# ---------------------------------------------------------------------------
# 前端页面（内嵌，保持单文件）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 主题与外观（主仪表盘 / 历史场次页共用同一份，避免两处变量漂移）
# ---------------------------------------------------------------------------
# 为什么单独抽出来：历史页本来各写一份 :root，改主题要动两处，很容易漏。
# 现在主页面用占位符替换（保持 raw 字符串，省掉对整页做 f-string 转义），
# 历史页因为是 f-string，直接插值即可。
#
# 新增主题的必要步骤（两处都要登记，缺一个就只是「选了没反应」）：
#   1. 在这里加一段 [data-theme="xxx"]，变量必须给全；
#   2. 到前端 THEMES 列表里登记同名条目（面板色卡靠它渲染）。
THEME_CSS = r"""
:root {
  --bg:#f5f6f7; --card:#fff; --line:#e3e5e8; --text:#1c1e21;
  --muted:#6b7280; --accent:#0d6efd; --accent-rgb:13,110,253;
  --ok:#198754; --warn:#fd7e14; --bad:#dc3545;
  --mono:ui-monospace,'SF Mono',Consolas,monospace;
  /* canvas 用色：canvas 不参与 CSS 继承，JS 读这些变量再传给 ctx */
  --cv-bowl:rgba(0,0,0,.028); --cv-ring:rgba(0,0,0,.075);
  --cv-edge:rgba(0,0,0,.18);  --cv-axis:rgba(0,0,0,.12);
  --cv-text:rgba(0,0,0,.5);   --cv-shadow:rgba(0,0,0,.22);
  --cv-grid:rgba(128,128,128,.18);
  /* 排版密度与读数缩放：个性化面板调的就是这几个 */
  --pad-body:12px; --gap-grid:10px; --pad-card:12px 14px; --gap-card:12px;
  --num-scale:1;
}
:root[data-density="compact"] {
  --pad-body:8px; --gap-grid:7px; --pad-card:8px 10px; --gap-card:8px;
}
:root[data-density="cozy"] {
  --pad-body:18px; --gap-grid:14px; --pad-card:16px 18px; --gap-card:16px;
}
/* —— 深色（跟随系统时的深色分支，也是「深色」预设本体） —— */
:root[data-theme="dark"] {
  --bg:#16181c; --card:#1e2126; --line:#2c3038; --text:#e8eaed;
  --muted:#9aa0a6; --accent:#2b7fff; --accent-rgb:43,127,255;
  --ok:#3fb950; --warn:#fd9843; --bad:#f85149;
  --cv-bowl:rgba(255,255,255,.04); --cv-ring:rgba(255,255,255,.10);
  --cv-edge:rgba(255,255,255,.26); --cv-axis:rgba(255,255,255,.16);
  --cv-text:rgba(255,255,255,.55); --cv-shadow:rgba(0,0,0,.55);
  --cv-grid:rgba(255,255,255,.14);
}
/* —— OLED 赛道黑：纯黑底 + 青柠高对比。夜间/副屏不刺眼，OLED 还省电 —— */
:root[data-theme="oled"] {
  --bg:#000; --card:#0a0b0d; --line:#1b1e22; --text:#eaf6f6;
  --muted:#7c8b91; --accent:#00e0c6; --accent-rgb:0,224,198;
  --ok:#2fe08a; --warn:#ffb020; --bad:#ff4d5e;
  --cv-bowl:rgba(255,255,255,.03); --cv-ring:rgba(255,255,255,.09);
  --cv-edge:rgba(0,224,198,.35);  --cv-axis:rgba(255,255,255,.14);
  --cv-text:rgba(234,246,246,.6); --cv-shadow:rgba(0,224,198,.35);
  --cv-grid:rgba(255,255,255,.12);
}
/* —— 米黄护眼：暖色纸质底，长时间看数据/复盘不刺眼 —— */
:root[data-theme="sepia"] {
  --bg:#f2e9d7; --card:#fdf8ec; --line:#e2d6bd; --text:#3b3128;
  --muted:#8a7a63; --accent:#b45309; --accent-rgb:180,83,9;
  --ok:#4a7a4f; --warn:#b26a12; --bad:#a83232;
  --cv-bowl:rgba(75,49,40,.04); --cv-ring:rgba(75,49,40,.09);
  --cv-edge:rgba(75,49,40,.2);  --cv-axis:rgba(75,49,40,.13);
  --cv-text:rgba(59,49,40,.6);  --cv-shadow:rgba(59,49,40,.2);
  --cv-grid:rgba(75,49,40,.12);
}
/* —— GT 竞速：碳纤黑 + GT 红，偏热血赛道向 —— */
:root[data-theme="racing"] {
  --bg:#0d0d0f; --card:#16161a; --line:#2b2b33; --text:#f4f4f6;
  --muted:#9a9aa6; --accent:#e10600; --accent-rgb:225,6,0;
  --ok:#21c25e; --warn:#ffb020; --bad:#ff6b6b;
  --cv-bowl:rgba(255,255,255,.035); --cv-ring:rgba(255,255,255,.10);
  --cv-edge:rgba(255,255,255,.24); --cv-axis:rgba(255,255,255,.15);
  --cv-text:rgba(244,244,246,.58); --cv-shadow:rgba(0,0,0,.6);
  --cv-grid:rgba(255,255,255,.13);
}
/* —— 冰川蓝：冷调浅色，清爽轻量向 —— */
:root[data-theme="glacier"] {
  --bg:#eaf2fb; --card:#fff; --line:#d2e3f3; --text:#10202f;
  --muted:#56718a; --accent:#0a84ff; --accent-rgb:10,132,255;
  --ok:#0f9b6c; --warn:#c2760a; --bad:#d92d20;
  --cv-bowl:rgba(16,32,47,.035); --cv-ring:rgba(16,32,47,.08);
  --cv-edge:rgba(16,32,47,.18); --cv-axis:rgba(16,32,47,.12);
  --cv-text:rgba(16,32,47,.55); --cv-shadow:rgba(16,32,47,.18);
  --cv-grid:rgba(16,32,47,.12);
}
/* —— HUD 荧光：黑底 + 荧光绿，赛博/HUD 观感 —— */
:root[data-theme="hud"] {
  --bg:#04100a; --card:#08180f; --line:#153a26; --text:#d9ffe9;
  --muted:#6fae8c; --accent:#39ff14; --accent-rgb:57,255,20;
  --ok:#2bff88; --warn:#ffe600; --bad:#ff2b5e;
  --cv-bowl:rgba(57,255,20,.05); --cv-ring:rgba(57,255,20,.14);
  --cv-edge:rgba(57,255,20,.4);  --cv-axis:rgba(57,255,20,.2);
  --cv-text:rgba(217,255,233,.6); --cv-shadow:rgba(57,255,20,.35);
  --cv-grid:rgba(57,255,20,.12);
}
/* —— 午夜紫：暗紫渐变感的长夜复盘向 —— */
:root[data-theme="midnight"] {
  --bg:#0c0a18; --card:#15122a; --line:#2b2450; --text:#eae6ff;
  --muted:#9c93c9; --accent:#9b6bff; --accent-rgb:155,107,255;
  --ok:#34d399; --warn:#fbbf24; --bad:#fb7185;
  --cv-bowl:rgba(234,230,255,.045); --cv-ring:rgba(234,230,255,.11);
  --cv-edge:rgba(234,230,255,.26); --cv-axis:rgba(234,230,255,.16);
  --cv-text:rgba(234,230,255,.6); --cv-shadow:rgba(0,0,0,.6);
  --cv-grid:rgba(234,230,255,.12);
}
/* 「跟随系统」跟随 prefers-color-scheme；其余预设不受系统切换影响。
   🔴 必须带 [data-theme="auto"]：如果像旧版那样裸写 @media + :root，
      用户显式选了浅色也会被系统深色覆盖——「选了没反应」就是这个坑。 */
@media (prefers-color-scheme: dark) {
  :root[data-theme="auto"] {
    --bg:#16181c; --card:#1e2126; --line:#2c3038; --text:#e8eaed;
    --muted:#9aa0a6; --accent:#2b7fff; --accent-rgb:43,127,255;
    --ok:#3fb950; --warn:#fd9843; --bad:#f85149;
    --cv-bowl:rgba(255,255,255,.04); --cv-ring:rgba(255,255,255,.10);
    --cv-edge:rgba(255,255,255,.26); --cv-axis:rgba(255,255,255,.16);
    --cv-text:rgba(255,255,255,.55); --cv-shadow:rgba(0,0,0,.55);
    --cv-grid:rgba(255,255,255,.14);
  }
}
"""

# 首屏防闪：在 <head> 里同步执行，先于 body 绘制把主题落到 <html> 上。
# 这段刻意保持「无依赖、无 try 之外的控制流」——它是页面上最先跑的代码，
# 一旦抛错整个首屏就白屏了。真正的偏好读写在页面底部 prefs 模块里。
THEME_BOOT_JS = r"""
(function () {
  try {
    var p = JSON.parse(localStorage.getItem('gt7_prefs_v1') || '{}');
    var d = document.documentElement;
    d.setAttribute('data-theme', p.theme || 'auto');
    d.setAttribute('data-density', p.density || 'normal');
    if (p.fontScale) {
      d.style.setProperty('--num-scale', (+p.fontScale / 100) || 1);
    }
    if (p.accent) {
      d.style.setProperty('--accent', p.accent);
      var h = p.accent.replace('#', '');
      if (h.length === 3) {
        h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      }
      var n = parseInt(h, 16);
      if (!isNaN(n)) {
        d.style.setProperty('--accent-rgb',
          ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255));
      }
    }
  } catch (e) { /* 隐私模式读不到 localStorage：用默认主题照常渲染 */ }
})();
"""


def build_page() -> str:
    # 用占位符替换而不是 f-string：整页 CSS/JS 里花括号上千个，
    # 转义一遍只会让人再也不敢改样式。
    return HTML_PAGE.replace("/*THEME_CSS*/", THEME_CSS) \
                    .replace("/*THEME_BOOT_JS*/", THEME_BOOT_JS)


def _page_shell(title: str, body: str) -> str:
    """历史页面的统一外壳。刻意与主仪表盘风格一致。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script>{THEME_BOOT_JS}</script>
<title>{title}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
{THEME_CSS}
body {{ font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
  background:var(--bg); color:var(--text); padding:16px; }}
.top {{ display:flex; align-items:center; gap:12px; margin-bottom:14px; }}
.top h1 {{ font-size:16px; font-weight:500; }}
.back {{ color:var(--accent); text-decoration:none; font-size:13px; }}
.card {{ background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:14px 16px; margin-bottom:10px; }}
h2 {{ font-size:12px; font-weight:500; color:var(--muted);
  text-transform:uppercase; letter-spacing:.6px; margin-bottom:10px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); }}
th {{ color:var(--muted); font-weight:500; font-size:12px; }}
tr:hover td {{ background:rgba(128,128,128,.06); cursor:pointer; }}
td.num {{ font-family:var(--mono); text-align:right; }}
.empty {{ text-align:center; padding:48px 20px; }}
.empty .big {{ font-size:40px; margin-bottom:12px; opacity:.5; }}
.empty h3 {{ font-size:15px; font-weight:500; margin-bottom:8px; }}
.empty p {{ font-size:13px; color:var(--muted); line-height:1.7; }}
.steps {{ text-align:left; max-width:460px; margin:20px auto 0;
  background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:16px 20px; }}
.steps li {{ list-style:none; font-size:13px; padding:6px 0;
  border-bottom:1px solid var(--line); }}
.steps li:last-child {{ border:none; }}
.steps b {{ color:var(--accent); }}
.kv {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; }}
.kv div {{ background:rgba(128,128,128,.08); border-radius:8px; padding:10px 12px; }}
.kv span {{ display:block; font-size:11px; color:var(--muted); }}
.kv b {{ font-size:19px; font-family:var(--mono); }}
/* —— 全屏加载遮罩：大场次解析要几秒，必须有反馈 —— */
#loadOv {{ position:fixed; inset:0; z-index:200; display:none;
  background:rgba(128,128,128,.35); backdrop-filter:blur(2px);
  align-items:center; justify-content:center; }}
#loadOv .box {{ background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:22px 34px; text-align:center;
  box-shadow:0 8px 30px rgba(0,0,0,.18); }}
.spin {{ width:26px; height:26px; margin:0 auto 12px;
  border:3px solid var(--line); border-top-color:var(--accent);
  border-radius:50%; animation:spin .8s linear infinite; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
#loadOv .msg {{ font-size:13.5px; }}
#loadOv .sub {{ font-size:11.5px; color:var(--muted); margin-top:6px; }}
</style></head><body>
<div id="loadOv"><div class="box">
  <div class="spin"></div>
  <div class="msg" id="loadMsg">加载中…</div>
  <div class="sub">大场次解析可能需要几秒</div>
</div></div>
<script>
function showLoad(msg) {{
  var ov = document.getElementById('loadOv');
  document.getElementById('loadMsg').textContent = msg || '加载中…';
  ov.style.display = 'flex';
}}
</script>
<div class="top">
  <a class="back" href="/">&larr; 返回仪表盘</a>
  <h1>{title}</h1>
</div>
{body}
</body></html>"""


_SESSIONS_PAGE_JS = """
<style>
.sbtn { border:1px solid var(--line); background:var(--card); color:inherit;
  border-radius:6px; padding:3px 9px; cursor:pointer; font-family:inherit;
  margin-left:4px; font-size:12.5px; }
.sbtn:hover { background:rgba(128,128,128,.16); }
.anom-badge { display:inline-block; margin-left:6px; padding:1px 7px; border-radius:10px;
  font-size:11px; font-weight:600; background:rgba(220,53,69,.15); color:var(--bad);
  border:1px solid var(--bad); vertical-align:middle; }
tr[data-anom="1"] td:first-child { box-shadow: inset 3px 0 0 var(--bad); }
</style>
<script>
// 点击场次链接立刻显示加载遮罩：
// 服务端解析大场次（几万帧 jsonl）要几秒，期间页面停在列表页
// 毫无反应，用户会以为没点上。遮罩会一直显示到新页面渲染完。
document.addEventListener('click', function (e) {
  var a = e.target.closest('a[href^="/session"]');
  if (a) showLoad('正在解析场次数据…');
});
function sessPost(file, action, value) {
  fetch('/api/v1/sessions/' + encodeURIComponent(file) + '/' + action, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: value})
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok) location.reload(); else alert(d.error || '操作失败');
  }).catch(function (e) { alert('请求失败：' + e); });
}
function sessRen(file) {
  var n = prompt('修改场次名称（留空取消）：');
  if (n === null) return;
  if (!n.trim()) { alert('名称不能为空'); return; }
  sessPost(file, 'rename', n.trim());
}
function loadTrashSettings() {
  fetch('/api/v1/settings').then(r => r.json()).then(function (d) {
    document.getElementById('trashInfo').textContent =
      '回收站现有 ' + d.trash_files + ' 个文件（' + d.trash_size_mb + ' MB）'
      + ' · 当前保留 ' + d.trash_retention_days + ' 天';
    document.getElementById('retDays').value = d.trash_retention_days;
    document.getElementById('nameTpl').value =
      d.session_name_template || '{车型} {时间} {最快圈}';
    // —— 回收站清单：恢复 / 彻底删除 ——
    var items = d.trash || [];
    document.getElementById('trashSummary').textContent =
      items.length ? items.length + ' 个文件' : '';
    var box = document.getElementById('trashList');
    if (!items.length) {
      box.innerHTML = '<p style="font-size:12.5px;color:var(--muted)">回收站是空的。</p>';
      return;
    }
    box.innerHTML = '<table><tr><th>文件</th><th style="text-align:right">大小</th>' +
      '<th style="text-align:right">删除时间</th><th style="text-align:right">操作</th></tr>' +
      items.map(function (t) {
        // 用 data-* 传值而不是把文件名塞进 onclick 的引号里：
        // 这段 JS 又是被 Python 三引号字符串包着的，单反斜杠会被 Python 先吃掉，
        // 生成 'trashPost('' + t.name' 这种坏语法（整段脚本直接不执行）。
        var nm = String(t.name).replace(/"/g, '&quot;');
        var anom = t.anomalous ? '1' : '0';
        var badge = t.anomalous ? ' <span class="anom-badge" title="异常场次：没有完成圈，或唯一圈不足 20 秒（菜单 / 停车场 / 刚点火的残片），会被自动归档到回收站">异常</span>' : '';
        return '<tr data-anom="' + anom + '"><td style="font-size:12px;font-family:var(--mono)">' + t.name + badge +
          '</td><td class="num">' + t.size_kb + ' KB</td><td class="num">' + t.modified +
          '</td><td class="num">' +
          '<button class="sbtn" data-trash="' + nm + '" data-act="restore">↩ 恢复</button>' +
          '<button class="sbtn" data-trash="' + nm + '" data-act="delete">✕ 彻底删除</button>' +
          '</td></tr>';
      }).join('') + '</table>';
    applyTrashFilters();
  });
}
document.getElementById('trashList').addEventListener('click', function (e) {
  var btn = e.target.closest('button[data-trash]');
  if (!btn) return;
  trashPost(btn.getAttribute('data-trash'), btn.getAttribute('data-act'));
});
function saveNameTpl() {
  var v = document.getElementById('nameTpl').value.trim();
  if (!v) { alert('模板不能为空（留空保存会恢复默认）'); return; }
  fetch('/api/v1/settings/name-template', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: v})
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok) location.reload();
    else alert(d.error || '保存失败');
  }).catch(function (e) { alert('请求失败：' + e); });
}
function trashPost(name, action) {
  if (action === 'delete' &&
      !confirm('彻底删除「' + name + '」？\\n（不可恢复，请确认不再需要）')) return;
  fetch('/api/v1/trash/' + encodeURIComponent(name) + '/' + action, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: '{}'
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok) loadTrashSettings(); else alert(d.error || '操作失败');
  }).catch(function (e) { alert('请求失败：' + e); });
}
function purgeTrash() {
  if (!confirm('清空回收站？\\n（所有已删除场次将被彻底删除，不可恢复）')) return;
  fetch('/api/v1/trash/purge', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: '{}'
  }).then(function (r) { return r.json(); }).then(function (d) {
    if (d.ok) { loadTrashSettings(); }
    else alert(d.error || '操作失败');
  }).catch(function (e) { alert('请求失败：' + e); });
}
function saveRetention() {
  var v = parseFloat(document.getElementById('retDays').value);
  if (isNaN(v) || v < 0 || v > 3650) {
    alert('天数需在 0~3650 之间（0 = 永不清理）'); return;
  }
  fetch('/api/v1/settings/trash-retention', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({value: v})
  }).then(function (r) { return r.json(); }).then(function (d) {
    document.getElementById('retMsg').textContent =
      d.ok ? '已保存，' + d.hint : (d.error || '保存失败');
    loadTrashSettings();
  }).catch(function (e) { alert('请求失败：' + e); });
}
loadTrashSettings();
function applySessFilters() {
  var fav = document.getElementById('favOnly').checked;
  document.querySelectorAll('#sessTable tr[data-fav]').forEach(function (tr) {
    tr.style.display = (!fav || tr.dataset.fav === '1') ? '' : 'none';
  });
}
function applyTrashFilters() {
  var anom = document.getElementById('anomTrashOnly').checked;
  document.querySelectorAll('#trashList tr[data-anom]').forEach(function (tr) {
    tr.style.display = (!anom || tr.dataset.anom === '1') ? '' : 'none';
  });
}
function sessDel(file) {
  if (!confirm('确定删除这场数据？\\n（移入下方「回收站」，可恢复或彻底删除）')) return;
  sessPost(file, 'delete', null);
}
</script>
"""


_COMPARE_TMPL = """
<style>
.cmp-svg { width:100%; height:auto; display:block; background:rgba(128,128,128,.06);
  border-radius:8px; }
#raceLineCv { width:100%; max-width:760px; display:block; margin:0 auto;
  border-radius:8px; background:rgba(128,128,128,.06); }
.pv-table td, .pv-table th { padding:5px 9px; font-size:12.5px; }
/* 🔴 .legend 样式只定义在主仪表盘页，历史页 shell 里没有——
   之前图例的 <i> 色块在这里渲染成 0×0，看起来就是光秃秃三个字。 */
.legend { display:flex; gap:16px; font-size:12px; color:var(--muted);
  justify-content:center; flex-wrap:wrap; margin-top:6px; }
.legend span { display:inline-flex; align-items:center; }
.legend i { display:inline-block; width:16px; height:4px; border-radius:2px;
  margin-right:5px; }
</style>
<div class="card">
  <h2>圈间对比分析 <span id="cmpMeta" style="float:right;font-weight:400"></span></h2>
  <p style="font-size:12px;color:var(--muted);margin-bottom:6px">
    曲线 = 最新圈相对参考圈的逐距离时间差：<b style="color:var(--bad)">正（上）= 丢时间</b>，
    <b style="color:var(--ok)">负（下）= 更快</b>。参考圈默认取最快圈。</p>
  <p style="font-size:11.5px;color:var(--muted);margin-bottom:4px">
    横轴 = 圈内行驶距离（<b>0 = 起点线</b>），刻度下方的灰色时间是参考圈跑到该位置的时刻；
    红点 = 丢时间最多处，绿点 = 领先最多处。</p>
  <svg id="diffSvg" class="cmp-svg" viewBox="0 0 720 236"></svg>
</div>
<div class="card">
  <h2>参考圈赛车线（第 <span id="rlLap">-</span> 圈）
    <select id="lapSel" onchange="lapSelChange(this.value)"
      style="float:right;font-weight:400;font-size:13px;padding:4px 8px;
             border:1px solid var(--line);border-radius:7px;
             background:var(--card);color:inherit;font-family:inherit"></select>
  </h2>
  <canvas id="raceLineCv" width="760" height="440"></canvas>
  <div class="legend" style="justify-content:center;margin-top:6px">
    <span><i style="background:var(--ok)"></i>油门</span>
    <span><i style="background:var(--bad)"></i>刹车</span>
    <span><i style="background:var(--accent)"></i>滑行</span>
  </div>
  <p class="dim" style="font-size:11.5px;margin-top:8px">
    可自选要看第几圈的赛车线。默认取最快圈；切换会刷新本页并把整页分析
    （时间差曲线 / 峰谷表）都以所选圈为基准重算。</p>
</div>
<div class="card">
  <h2>哪里快 / 哪里慢 —— 关键点对比</h2>
  <div id="pvSummary" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px"></div>
  <table class="pv-table">
    <thead><tr><th>距离 (m)</th><th>关键点</th>
      <th style="text-align:right">最新圈</th>
      <th style="text-align:right">最快圈</th>
      <th style="text-align:right">差值</th></tr></thead>
    <tbody id="pvBody"></tbody>
  </table>
  <p class="dim" style="font-size:11.5px;margin-top:8px">
    关键点 = 直道尾速（峰）与弯心速度（谷），按距离配对。差值 = 最新圈 − 最快圈：
    <b style="color:var(--ok)">绿 +</b> 最新圈更快，
    <b style="color:var(--bad)">红 −</b> 更慢。</p>
</div>
<script>
const CMP = __DATA__;
(function () {
  if (!CMP || CMP.error || !CMP.laps_analyzed) {
    const m = document.getElementById('cmpMeta');
    if (m) m.textContent = '圈数据不足，无法对比（至少跑完一圈的 30 帧）';
    return;
  }
  document.getElementById('cmpMeta').textContent =
    '参考圈：第 ' + CMP.ref_lap + ' 圈 · 对比：第 ' + CMP.cur_lap
    + ' 圈 · 共分析 ' + CMP.laps_analyzed + ' 圈';
  document.getElementById('rlLap').textContent = CMP.ref_lap;

  // —— 参考圈选择器：列出所有有效圈，默认选中当前参考圈 ——
  window.lapSelChange = function (v) {
    const u = new URL(location.href);
    u.searchParams.set('ref_lap', v);
    showLoad('正在按第 ' + v + ' 圈重新分析…');
    location.href = u.toString();
  };
  (function fillLapSel() {
    const sel = document.getElementById('lapSel');
    if (!sel) return;
    const sum = CMP.lap_summary || [];
    if (sum.length < 2) {          // 只有一圈时没必要选
      sel.style.display = 'none';
      return;
    }
    // 🔴 「最快」标注必须按真实圈速算，不能贴在当前参考圈上——
    //    否则用户切到别的圈，选择器会把那一圈也叫「最快」（实测踩过）。
    //    口径与左侧圈速表一致：≥20s 才算有效圈。
    const valid = sum.filter(s => s.duration_s >= 20);
    const fastestLap = valid.length
      ? valid.reduce((a, s) => s.duration_s < a.duration_s ? s : a).lap : null;
    sum.forEach(function (s) {
      const o = document.createElement('option');
      const tags = [];
      if (s.lap === fastestLap) tags.push('最快');
      if (s.lap === CMP.ref_lap && s.lap !== fastestLap) tags.push('参考圈');
      o.value = s.lap;
      o.textContent = '第 ' + s.lap + ' 圈 · ' + s.duration_s.toFixed(1) + 's'
        + (tags.length ? '（' + tags.join('·') + '）' : '');
      if (s.lap === CMP.ref_lap) o.selected = true;
      sel.appendChild(o);
    });
  })();

  // —— 时间差曲线（带坐标参考：X=圈内距离+参考圈时刻，Y=毫秒） ——
  const d = CMP.time_diff, svg = document.getElementById('diffSvg');
  if (d.grid && d.grid.length > 1) {
    const W = 720, H = 236;
    const L = 58, R = W - 12, T = 16, B = H - 40;   // 绘图区
    const amax = Math.max(50, ...d.diff_ms.map(v => Math.abs(v)));
    const x = i => L + (R - L) * i / (d.grid.length - 1);
    const y = v => B / 2 + T / 2 - ((B - T) / 2) * v / amax;
    const fms = v => {
      const s = v > 0 ? '+' : (v < 0 ? '-' : '');
      const a = Math.abs(v);
      return s + (a >= 10000 ? (a / 1000).toFixed(1) + 's' : Math.round(a) + 'ms');
    };
    const fdist = m => m >= 1000
      ? (m / 1000).toFixed(2).replace(/\.?0+$/, '') + 'k' : Math.round(m) + 'm';
    const fref = ms => {
      const t = ms / 1000;
      return t < 60 ? t.toFixed(1) + 's'
        : Math.floor(t / 60) + ':' + String(Math.round(t % 60)).padStart(2, '0');
    };
    let html = '';
    // Y 网格：±amax / ±amax/2 / 0
    for (const f of [1, 0.5, 0, -0.5, -1]) {
      const v = amax * f, yy = y(v).toFixed(1);
      html += '<line x1="' + L + '" y1="' + yy + '" x2="' + R + '" y2="' + yy
        + '" stroke="rgba(128,128,128,.22)" stroke-width="1"/>';
      html += '<text x="' + (L - 6) + '" y="' + (+yy + 3.5)
        + '" text-anchor="end" font-size="10" fill="var(--muted)">'
        + (f === 0 ? '0' : fms(v)) + '</text>';
    }
    // X 网格 + 双行标注：圈内距离 / 参考圈到该位置的时刻
    const refT = d.ref_t_rel_ms || [];
    for (let k = 0; k <= 8; k++) {
      const i = Math.round(k / 8 * (d.grid.length - 1));
      const xx = x(i).toFixed(1);
      // 两端刻度向内对齐，避免贴边被裁
      const anchor = k === 0 ? 'start' : (k === 8 ? 'end' : 'middle');
      html += '<line x1="' + xx + '" y1="' + T + '" x2="' + xx + '" y2="' + B
        + '" stroke="rgba(128,128,128,.18)" stroke-width="1"/>';
      html += '<text x="' + xx + '" y="' + (B + 15) + '" text-anchor="' + anchor
        + '" font-size="10" fill="var(--muted)">' + fdist(d.grid[i]) + '</text>';
      if (refT.length) {
        html += '<text x="' + xx + '" y="' + (B + 28) + '" text-anchor="' + anchor
          + '" font-size="9.5" fill="var(--muted)" opacity=".75">'
          + fref(refT[i]) + '</text>';
      }
    }
    // 零线加粗（虚线）：上下分界
    html += '<line x1="' + L + '" y1="' + y(0) + '" x2="' + R + '" y2="' + y(0)
      + '" stroke="rgba(128,128,128,.55)" stroke-dasharray="4 4"/>';
    // 差值曲线
    for (let i = 1; i < d.diff_ms.length; i++) {
      const v = (d.diff_ms[i] + d.diff_ms[i - 1]) / 2;
      html += '<line x1="' + x(i - 1).toFixed(1) + '" y1="' + y(d.diff_ms[i - 1]).toFixed(1)
        + '" x2="' + x(i).toFixed(1) + '" y2="' + y(d.diff_ms[i]).toFixed(1)
        + '" style="stroke:' + (v >= 0 ? 'var(--bad)' : 'var(--ok)')
        + '" stroke-width="1.6"/>';
    }
    // 最大得失标记：一眼定位「在哪里丢/赚了多少」
    const iMax = d.diff_ms.reduce((a, v, i) => v > d.diff_ms[a] ? i : a, 0);
    const iMin = d.diff_ms.reduce((a, v, i) => v < d.diff_ms[a] ? i : a, 0);
    const mark = (i, color, above) => {
      const mx = x(i), my = y(d.diff_ms[i]);
      const label = fms(d.diff_ms[i]) + '@' + fdist(d.grid[i]);
      const ty = above ? Math.max(T + 9, my - 7) : Math.min(B - 3, my + 14);
      const tx = Math.min(Math.max(mx, L + 34), R - 34);
      return '<circle cx="' + mx.toFixed(1) + '" cy="' + my.toFixed(1)
        + '" r="3" style="fill:' + color + '"/>'
        + '<text x="' + tx.toFixed(1) + '" y="' + ty.toFixed(1)
        + '" text-anchor="middle" font-size="9.5" style="fill:' + color
        + '">' + label + '</text>';
    };
    if (d.diff_ms[iMax] > 0) html += mark(iMax, 'var(--bad)', true);
    if (d.diff_ms[iMin] < 0) html += mark(iMin, 'var(--ok)', false);
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.innerHTML = html;
  }

  // —— 三色赛车线 ——
  const cv = document.getElementById('raceLineCv'), ctx = cv.getContext('2d');
  const segs = (CMP.race_line || {}).segments || [];
  ctx.clearRect(0, 0, cv.width, cv.height);
  // 取当前主题的实际颜色：canvas 不继承 CSS 变量，只能手动读一次
  const cvar = n => getComputedStyle(document.documentElement)
    .getPropertyValue(n).trim() || '#888';
  const cmap = {brake: cvar('--bad'), throttle: cvar('--ok'), coast: cvar('--accent')};
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
  segs.forEach(s => s.pts.forEach(p => {
    if (p[0] == null) return;
    if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0];
    if (p[1] < z0) z0 = p[1]; if (p[1] > z1) z1 = p[1];
  }));
  if (x1 > x0 && z1 > z0) {
    const PAD = 24;
    const sc = Math.min((cv.width - 2*PAD) / (x1 - x0), (cv.height - 2*PAD) / (z1 - z0));
    const ox = (cv.width - (x1 - x0) * sc) / 2 - x0 * sc;
    const oy = (cv.height - (z1 - z0) * sc) / 2 - z0 * sc;
    const px = v => ox + v * sc, py = v => cv.height - (oy + v * sc);
    ctx.lineCap = 'round'; ctx.lineWidth = 3;
    segs.forEach(s => {
      ctx.strokeStyle = cmap[s.color] || '#888';
      ctx.beginPath();
      let started = false;
      s.pts.forEach(p => {
        if (p[0] == null) return;
        if (!started) { ctx.moveTo(px(p[0]), py(p[1])); started = true; }
        else ctx.lineTo(px(p[0]), py(p[1]));
      });
      ctx.stroke();
    });
  }

  // —— 关键点对比表（服务端已按圈内相对位置配好对）——
  // 口径：差值 = 最新圈 − 最快圈。正 = 最新圈更快（绿），负 = 更慢（红）。
  const rows = CMP.pv_pairs || [];
  const kindTxt = k => k === 'peak' ? '直道尾速' : '弯心速度';
  const dTxt = d => (d > 0 ? '+' : '') + d.toFixed(1);
  const dColor = d => d > 0.05 ? 'var(--ok)' : (d < -0.05 ? 'var(--bad)' : 'var(--muted)');
  document.getElementById('pvBody').innerHTML = rows.map(p =>
    '<tr><td>' + p.distance + '</td>'
    + '<td>' + kindTxt(p.kind) + '</td>'
    + '<td class="num">' + p.speed_cur + '</td>'
    + '<td class="num">' + p.speed_ref + '</td>'
    + '<td class="num"><b style="color:' + dColor(p.delta) + '">'
    + dTxt(p.delta) + '</b></td></tr>'
  ).join('') || '<tr><td colspan="5">无</td></tr>';

  // —— 摘要：这一圈最大的优势与劣势（一眼看出差距在哪）——
  const sum = document.getElementById('pvSummary');
  if (rows.length) {
    const best = rows.reduce((a, p) => p.delta > a.delta ? p : a);
    const worst = rows.reduce((a, p) => p.delta < a.delta ? p : a);
    const distTxt = v => v >= 1000 ? (v / 1000).toFixed(2) + 'k' : Math.round(v);
    const chip = p => '<span style="font-size:12px;padding:4px 10px;'
      + 'border-radius:14px;background:rgba(128,128,128,.12)">'
      + (p.delta > 0 ? '领先最多' : '落后最多') + ' <b style="color:'
      + (p.delta > 0 ? 'var(--ok)' : 'var(--bad)') + '">' + dTxt(p.delta)
      + ' km/h</b> <span style="color:var(--muted)">@'
      + distTxt(p.distance) + 'm</span></span>';
    sum.innerHTML = (best.delta > 0 ? chip(best) : '')
      + (worst.delta < 0 ? chip(worst) : '');
    sum.style.display = sum.innerHTML ? 'flex' : 'none';
  }
})();
</script>
"""


def build_sessions_page(hist: Path) -> str:
    """历史场次列表页。空状态要说清为什么空、以及怎么让它有数据。"""
    sessions = list_sessions(hist)

    if not sessions:
        body = f"""
<div class="card empty">
  <div class="big">&#128203;</div>
  <h3>还没有任何场次记录</h3>
  <p>这是正常的 —— 数据目录里还没有采集到遥测。<br>
     开始跑一圈就会自动出现，不需要手动操作。</p>
  <div class="steps">
    <li><b>1.</b> 确认 PS5 和服务器都在 192.168.43.x 同一网段</li>
    <li><b>2.</b> 进 PS5 → 设置 → 网络，打开 GT7 的遥测功能</li>
    <li><b>3.</b> 进一局时间赛，<b>车辆开动</b>后才开始记录
        <br><span style="color:var(--muted)">（菜单和停车场不算）</span></li>
    <li><b>4.</b> 回到这里刷新，或看
        <a href="/" style="color:var(--accent)">实时仪表盘</a>确认是否在收数据</li>
  </div>
</div>"""
        return _page_shell("历史场次", body)

    rows = []
    for s in sessions:
        disp = s.get("display_name") or s["circuit"]
        disp_attr = disp.replace('"', "&quot;")
        star = ' <span style="color:#d4a017">★</span>' if s["favorite"] else ""
        anom = s.get("anomalous")
        badge = ' <span class="anom-badge">异常</span>' if anom else ""
        rows.append(
            f"""<tr data-fav="{'1' if s['favorite'] else '0'}" data-anom="{'1' if anom else '0'}" data-file="{s['file']}">
      <td><b><a href="/session?file={s['file']}"
         style="color:var(--accent)">{disp}</a></b>{star}{badge}</td>
      <td style="font-size:12.5px">{s.get('car_name') or '<span class="dim">-</span>'}</td>
      <td>{s['modified']}</td>
      <td class="num">{s['size_kb']} KB</td>
      <td class="num">
        <button class="sbtn" title="收藏"
          onclick="sessPost('{s['file']}','favorite',{str(not s['favorite']).lower()})">{'★' if s['favorite'] else '☆'}</button>
        <button class="sbtn" title="改名"
          onclick="sessRen('{s['file']}')">✎</button>
        <button class="sbtn" title="删除"
          onclick="sessDel('{s['file']}')">🗑</button>
        <button class="sbtn" title="下载原始数据"
          onclick="location.href='/api/v1/sessions/{s['file']}/download'">⬇</button>
      </td>
    </tr>"""
        )

    body = f"""
<div class="card">
  <h2>共 {len(sessions)} 个场次
    <label style="float:right;font-weight:400;font-size:12px;cursor:pointer">
      <input type="checkbox" id="favOnly" onchange="applySessFilters()"> 只看收藏 ★
    </label></h2>
  <p style="font-size:12.5px;color:var(--muted);margin:-4px 0 8px">
    异常场次（菜单/停车场/刚点火残片：无完成圈或唯一圈 &lt;20s）会在加载时自动归档到下方「回收站」，可恢复。</p>
  <table id="sessTable">
    <tr><th>场次</th><th>车型</th><th>采集时间</th>
        <th style="text-align:right">大小</th>
        <th style="text-align:right">操作</th></tr>
    {''.join(rows)}
  </table>
</div>

<div class="card">
  <h2>显示设置</h2>
  <p style="font-size:12.5px;color:var(--muted)">
    场次默认按模板命名（手动改过名的场次不受影响）。可用占位符：
    <b>{{车型}}</b> <b>{{时间}}</b> <b>{{最快圈}}</b> <b>{{场次}}</b></p>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <input id="nameTpl" placeholder="{{车型}} {{时间}} {{最快圈}}"
      style="flex:1;min-width:220px;padding:7px 10px;border:1px solid var(--line);
             border-radius:7px;background:var(--card);color:inherit;
             font-family:inherit;font-size:13px">
    <button onclick="saveNameTpl()"
      style="border:1px solid var(--line);background:var(--card);color:inherit;
             padding:7px 14px;border-radius:7px;cursor:pointer;
             font-family:inherit;font-size:13px">保存模板</button>
    <span id="tplMsg" style="font-size:12px;color:var(--muted)"></span>
  </div>
</div>

<div class="card">
  <h2>回收站
    <label style="float:right;font-weight:400;font-size:12px;cursor:pointer;margin-left:12px">
      <input type="checkbox" id="anomTrashOnly" onchange="applyTrashFilters()"> 只看异常场次</label>
    <span id="trashSummary" style="float:right;font-weight:400;
      font-size:12px;color:var(--muted)"></span></h2>
  <p style="font-size:12.5px;color:var(--muted)">
    删除的场次先移入 <b>data/_trash/</b>，超过保留期后由服务自动真删
    （每小时检查一次，设 <b>0</b> 天 = 永不自动清理）；也可以在这里手动清理。<br>
    带 <span class="anom-badge" style="margin-left:0">异常</span> 标记的是「没有完成圈，
    或唯一圈不足 20 秒」的残片场次（菜单 / 停车场 / 刚点火）——它们会被自动归档到这里，
    可恢复，也可彻底删除。</p>
  <div id="trashInfo" style="font-size:13px;margin:10px 0">加载中…</div>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <label style="font-size:13px">保留天数：
      <input type="number" id="retDays" min="0" max="3650" step="1"
        style="width:76px;padding:6px 8px;border:1px solid var(--line);
               border-radius:7px;background:var(--card);color:inherit;
               font-family:inherit">
    </label>
    <button onclick="saveRetention()"
      style="border:1px solid var(--line);background:var(--card);color:inherit;
             padding:7px 14px;border-radius:7px;cursor:pointer;
             font-family:inherit;font-size:13px">保存</button>
    <button onclick="purgeTrash()"
      style="border:1px solid var(--bad);background:var(--card);color:var(--bad);
             padding:7px 14px;border-radius:7px;cursor:pointer;
             font-family:inherit;font-size:13px">清空回收站</button>
    <span id="retMsg" style="font-size:12px;color:var(--muted)"></span>
  </div>
  <div id="trashList" style="margin-top:10px"></div>
</div>""" + _SESSIONS_PAGE_JS
    return _page_shell("历史场次", body)


def build_session_page(path: Path, stats: dict, ref_lap_no: int | None = None) -> str:
    """单场次详情：把离线统计展示成人能读的页面。"""
    import html as _html
    try:
        cmp_data = compare_session(path, ref_lap_no=ref_lap_no)
    except Exception:
        cmp_data = {}
    cmp_json = json.dumps(cmp_data, ensure_ascii=False).replace("</", "<\\/")

    if "error" in stats:
        return _page_shell("场次详情", f'<div class="card"><h2>解析失败</h2>{stats["error"]}</div>')

    hdr = stats.get("header", {})
    laps = stats.get("laps", [])
    best = stats.get("best_lap")

    lap_rows = []
    for lp in laps:
        is_best = best and lp["lap"] == best["lap"]
        if lp.get("incomplete"):
            # 数据不足 20 秒，这一圈还没跑完
            note = '<span style="color:var(--warn)">数据不足，未跑完</span>'
        elif is_best:
            note = '<b style="color:var(--ok)">最快圈</b>'
        else:
            note = ""
        lap_rows.append(
            f"""<tr>
      <td>第 {lp['lap']} 圈</td>
      <td class="num">{lp['time']:.3f}s</td>
      <td>{note}</td>
    </tr>"""
        )

    layouts = stats.get("layouts", {})
    layout_txt = "、".join(f"{k}×{v}" for k, v in layouts.items()) or "未知"
    coord = "含车身坐标" if stats.get("has_coords") else "无车身坐标"

    body = f"""
<div class="card">
  <h2>概览</h2>
  <div class="kv">
    <div><span>车型</span><b style="font-size:14px">{_html.escape(stats.get('car_name') or '未识别')}</b></div>
    <div><span>总帧数</span><b>{stats.get('frame_count', 0)}</b></div>
    <div><span>时长</span><b>{stats.get('duration', 0)}s</b></div>
    <div><span>最高速度</span><b>{stats.get('max_speed', 0)}</b></div>
    <div><span>平均速度</span><b>{stats.get('avg_speed', 0)}</b></div>
    <div><span>最高转速</span><b>{int(stats.get('max_rpm', 0))}</b></div>
    <div><span>包格式</span><b style="font-size:14px">{_html.escape(layout_txt)}</b></div>
  </div>
  <p style="margin-top:12px;font-size:12px;color:var(--muted)">
    {_html.escape(hdr.get('source_ip') or '来源未知')} · {coord}</p>
</div>

<div class="card">
  <h2>圈速</h2>
  {f'''<table><tr><th>圈</th><th style="text-align:right">用时</th><th></th></tr>
      {''.join(lap_rows)}</table>''' if lap_rows else
     '<p style="font-size:13px;color:var(--muted)">未能识别出完整的圈（可能只跑了几秒）</p>'}
</div>"""
    return _page_shell(f"场次 · {_html.escape(str(hdr.get('circuit') or 'unknown'))}",
                       body + _COMPARE_TMPL.replace('__DATA__', cmp_json))


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script>/*THEME_BOOT_JS*/</script>
<title>GT7 遥测仪表盘</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
/*THEME_CSS*/
body { font-family:system-ui,-apple-system,'Segoe UI',sans-serif;
  background:var(--bg); color:var(--text); padding:var(--pad-body); }
.bar { display:flex; align-items:center; gap:16px; flex-wrap:wrap;
  background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:10px 16px; margin-bottom:var(--gap-grid);
  position:sticky; top:var(--pad-body); z-index:10;
  backdrop-filter:blur(10px);
  box-shadow:0 4px 18px rgba(0,0,0,.06); }
/* 顶栏导航：胶囊式，hover 染主题色（深浅色主题通用） */
.bar .nav { margin-left:auto; display:flex; gap:4px; }
.bar .nav a { color:var(--muted); text-decoration:none; font-size:13px;
  padding:5px 12px; border-radius:7px; transition:.12s; }
.bar .nav a:hover { color:var(--accent); background:rgba(var(--accent-rgb),.12); }
.dot { width:10px; height:10px; border-radius:50%; background:var(--muted); }
.dot.on { background:var(--ok); box-shadow:0 0 0 3px rgba(25,135,84,.2); }
.dot.off { background:var(--bad); }
.dot.wait { background:var(--warn); animation:pulse 1.2s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
.stat { font-size:13px; color:var(--muted); }
.stat b { color:var(--text); font-variant-numeric:tabular-nums;
  font-family:var(--mono); font-weight:600; margin-left:4px; }
.grid { display:grid; grid-template-columns:1fr 1fr; gap:var(--gap-grid); }
@media(max-width:820px){ .grid{grid-template-columns:1fr} }
/* 三栏行：圈速 | G力球 | G-G散点。用 auto-fit 让它窄屏自动换行 */
.grid3 { grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); }
.card { background:var(--card); border:1px solid var(--line);
  border-radius:10px; padding:var(--pad-card); }
.card h2 { font-size:12px; font-weight:500; color:var(--muted);
  text-transform:uppercase; letter-spacing:.6px; margin-bottom:10px; }
.dials { display:flex; align-items:center; justify-content:space-around; }
.gauge { position:relative; width:132px; height:132px; }
.gauge svg { transform:rotate(135deg); }
/* SVG 的 presentation attribute 里不能用 var()，转速环的上色只能走 CSS */
.gauge .bg-ring { stroke:var(--cv-ring); }
#rpmArc { stroke:var(--accent); transition:stroke .15s; }
.gauge-val { position:absolute; inset:0; display:flex;
  flex-direction:column; align-items:center; justify-content:center; }
/* 读数字号统一挂 --num-scale（个性化面板的「读数缩放」只改这一个变量），
   避免面板去逐个重写这里的 font-size。 */
.gauge-val b { font-size:calc(22px * var(--num-scale)); font-family:var(--mono); }
.gauge-val span { font-size:11px; color:var(--muted); }
.speed { text-align:center; }
.speed b { font-size:calc(34px * var(--num-scale)); font-family:var(--mono); }
.speed span { display:block; font-size:11px; color:var(--muted); }
.bar-row { display:flex; align-items:center; gap:8px; margin-bottom:7px;
  font-size:12px; }
.bar-row label { width:52px; color:var(--muted); flex-shrink:0; }
.track { flex:1; height:12px; background:rgba(128,128,128,.18);
  border-radius:6px; overflow:hidden; }
.fill { height:100%; border-radius:6px; transition:width .08s linear; }
.fill.t { background:var(--ok); } .fill.b { background:var(--bad); }
.fill.lat { background:var(--accent); } .fill.lon { background:var(--warn); }
/* 油量：低于 20% 变红提示（比赛最后阶段要留意） */
.fill.fuel { background:linear-gradient(90deg,var(--accent),rgba(var(--accent-rgb),.55)); }
.fill.fuel.low { background:var(--bad); }
.fill.turbo { background:#20c997; }
.shiftlamp { display:flex; gap:5px; justify-content:center; margin:9px 0 2px; }
.shiftlamp i { width:15px; height:7px; border-radius:3px;
  background:rgba(128,128,128,.28); transition:background .05s; }
.shiftlamp.hot i { background:#dc3545; box-shadow:0 0 7px #dc3545; }
.shiftlamp i.on { background:#28a745; box-shadow:0 0 7px #28a745; }
.eng-row { display:flex; justify-content:space-between; font-size:13px;
  padding:5px 0; border-bottom:1px solid rgba(128,128,128,.12); }
.eng-row:last-child { border-bottom:none; }
.eng-row b { font-family:var(--mono); }
.eng-row b.ok { color:var(--ok); } .eng-row b.hot { color:var(--bad); }
/* 🔴 布局必须写在 CSS 里：JS 用 style.display='' 恢复显示时，
   会把内联的 display:flex 一起清掉 → 回退成 block，四轮挤成一串。 */
.susp { display:flex; justify-content:space-around; margin-top:10px;
  font-size:11.5px; color:var(--muted); flex-wrap:wrap; gap:4px 0; }
/* 圈速面板 */
.laps { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
.laps > div { background:rgba(128,128,128,.1); border-radius:8px; padding:8px 10px; }
.laps span { display:block; font-size:11px; color:var(--muted); }
.laps b { font-size:calc(15px * var(--num-scale)); font-family:var(--mono); }
/* 每圈圈速列表（新圈在上，最快圈绿色★） */
.laplist { margin-top:10px; max-height:172px; overflow-y:auto; }
.laprow { display:flex; justify-content:space-between; align-items:center;
  padding:4px 9px; border-bottom:1px solid var(--line); font-size:12.5px; }
.laprow span { color:var(--muted); }
.laprow b { font-family:var(--mono); font-weight:500; }
.laprow.best b { color:#198754; }
.laprow.best span::after { content:' ★最快'; color:#198754; font-size:10px; }
.laprow.empty { color:var(--muted); justify-content:center;
  border-bottom:none; padding:10px 0; }
#map { border-radius:8px; background:rgba(128,128,128,.06); }
#gg { border-radius:8px; }
/* G 力球读数 */
.ggreadout { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:8px; }
.ggreadout span { background:rgba(128,128,128,.1); border-radius:8px;
  padding:6px 4px; text-align:center; font-size:11px; color:var(--muted); }
.ggreadout b { display:block; font-size:calc(15px * var(--num-scale)); font-family:var(--mono);
  color:var(--text); margin-top:2px; }

/* ---------- 术语说明：右侧滑入面板 ---------- */
.apilist { list-style:none; }
.apilist li { padding:7px 0; border-bottom:1px solid var(--line); font-size:12.5px; }
.apilist li:last-child { border-bottom:none; }
.apilist b { font-family:var(--mono); color:var(--accent); font-size:12px; }
.codebox { background:rgba(128,128,128,.12); border-radius:7px; padding:9px 11px;
  font-family:var(--mono); font-size:12px; overflow-x:auto; word-break:break-all; }
/* 🔴 层级必须拆开：遮罩(55) < 编辑中的卡片(60) < 面板(70)。
   上一版把整个 wrap 设 z-index:50，卡片抬到 60 后连面板一起被压住——
   布局面板自己反而点不到了（DC 实测截图）。
   所以 wrap 只做显隐容器，遮罩和面板各自 fixed + 独立 z-index。 */
.gloss-wrap { display:none; }
.gloss-wrap.open { display:block; }
.gloss-mask { position:fixed; inset:0; background:rgba(0,0,0,.38); z-index:55; }
.gloss { position:fixed; top:0; right:0; height:100%; z-index:70;
  width:min(440px, 94vw); background:var(--card);
  border-left:1px solid var(--line); box-shadow:-10px 0 34px rgba(0,0,0,.22);
  display:flex; flex-direction:column; animation:glossIn .22s ease-out; }
@keyframes glossIn { from { transform:translateX(26px); opacity:.5 }
                     to { transform:none; opacity:1 } }
.gloss header { display:flex; align-items:center; justify-content:space-between;
  padding:14px 18px; border-bottom:1px solid var(--line); flex-shrink:0; }
.gloss header h3 { font-size:15px; font-weight:600; margin:0; }
.gloss header button { border:none; background:transparent; cursor:pointer;
  font-size:16px; color:var(--muted); padding:4px 9px; border-radius:6px;
  font-family:inherit; }
.gloss header button:hover { background:rgba(128,128,128,.14); }
.gloss-body { overflow-y:auto; padding:2px 18px 30px; }
.gloss-body section { padding:14px 0; border-bottom:1px solid var(--line); }
.gloss-body section:last-child { border-bottom:none; }
.gloss-body h4 { font-size:13px; font-weight:600; margin:0 0 7px;
  color:var(--accent); }
/* ---------- 布局系统：卡片可拖拽 / 显隐 / 多布局 ---------- */
.cgrid { display:grid; grid-template-columns:1fr 1fr; gap:var(--gap-card); }
.cgrid .card { margin-bottom:0; min-width:0; overflow:hidden;
  transition:border-color .15s, box-shadow .15s; }
.cgrid .card:hover { border-color:rgba(var(--accent-rgb),.45); }
.cgrid .card.span2 { grid-column:1 / -1; }
.cgrid .card.editmode { outline:2px dashed var(--accent); outline-offset:2px; }
.cgrid .card.editmode > h2 { cursor:move; }
.cgrid .card.editmode > h2::before { content:'⠿ '; color:var(--muted); }
@media(max-width:820px){ .cgrid{grid-template-columns:1fr}
  .cgrid .card.span2{grid-column:auto} }
/* 🔴 布局编辑时卡片必须抬到遮罩之上：
   布局面板是模态弹窗（z-index:50，全屏遮罩），卡片原本被压在下面——
   看得见（遮罩半透明）但摸不着：按下/拖动全落在遮罩上，
   松开触发遮罩的 onclick 关闭面板。这就是「一拖面板就退出」的根因。 */
body.layout-editing #cardGrid { position:relative; z-index:60; }
body.layout-editing .cgrid .card.editmode {
  box-shadow:0 10px 34px rgba(0,0,0,.4); }
.trow { display:flex; justify-content:space-between; align-items:center;
  padding:5px 0; border-bottom:1px solid var(--line); }
.trow:last-child { border-bottom:none; }
.trow label { flex:1; font-size:13px; cursor:pointer; }
.trow select { border:1px solid var(--line); border-radius:6px;
  background:var(--card); color:inherit; font-family:inherit;
  padding:3px 6px; font-size:12px; }
.rowbtns { display:flex; gap:8px; margin:9px 0; }
.rowbtns button { flex:1; border:1px solid var(--line); background:var(--card);
  color:inherit; padding:7px 8px; border-radius:7px; cursor:pointer;
  font-family:inherit; font-size:12.5px; }
.rowbtns button:disabled { opacity:.45; cursor:not-allowed; }
#layoutSel, #layoutName { width:100%; padding:8px 10px; margin-top:6px;
  border:1px solid var(--line); border-radius:7px;
  background:var(--card); color:inherit; font-family:inherit; font-size:13px; }
/* ---------- 个性化设置面板 ---------- */
.themepick { display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-top:4px; }
.tbox { display:flex; flex-direction:column; gap:5px; cursor:pointer;
  border:1px solid var(--line); border-radius:9px; padding:7px 9px;
  background:var(--card); color:var(--text);
  font-family:inherit; font-size:12px; text-align:left; transition:.12s; }
.tbox:hover { border-color:rgba(var(--accent-rgb),.55); }
.tbox.on { outline:2px solid var(--accent); outline-offset:1px; }
.tbox .sw { display:flex; gap:3px; }
.tbox .sw i { width:15px; height:15px; border-radius:5px;
  border:1px solid rgba(128,128,128,.35); }
.tbox b { font-size:12px; font-weight:600; }
.tbox span { font-size:10.5px; color:var(--muted); line-height:1.4; }
.accentpick { display:flex; flex-wrap:wrap; gap:7px; align-items:center; }
.accentpick button { width:24px; height:24px; border-radius:50%; padding:0;
  border:2px solid transparent; cursor:pointer; transition:.12s; }
.accentpick button.on { border-color:var(--text); transform:scale(1.08); }
input[type="range"] { width:100%; accent-color:var(--accent); margin-top:6px; }
input[type="color"] { width:32px; height:24px; padding:0; vertical-align:middle;
  border:1px solid var(--line); border-radius:6px; background:none; cursor:pointer; }
.prefjson { width:100%; min-height:62px; resize:vertical; font-family:var(--mono);
  font-size:11px; line-height:1.5; border:1px solid var(--line); border-radius:7px;
  background:rgba(128,128,128,.06); color:inherit; padding:7px 8px; margin-top:6px; }
.trow > select { min-width:96px; }
.trow output { font-family:var(--mono); font-size:11.5px; color:var(--muted); }
.gloss-body p { font-size:13px; line-height:1.75; margin:0 0 6px; }
.gloss-body p.dim { font-size:12px; color:var(--muted); line-height:1.7; }
.gloss-body ul { margin:4px 0 6px; padding-left:19px; }
.gloss-body li { font-size:13px; line-height:1.8; }
.gloss-body b { font-weight:600; }
.k { display:inline-block; padding:0 5px; border-radius:4px; font-size:11px;
  line-height:16px; color:#fff; }
.k.blue { background:var(--accent) } .k.green { background:var(--ok) }
.k.orange { background:#fd7e14 } .k.red { background:#dc3545 }
.bar-row output { width:42px; text-align:right; font-family:var(--mono);
  font-size:11px; color:var(--muted); }
/* 实时曲线：分面小图（small multiples），每个通道独立 Y 轴与刻度 */
.chsvg { display:block; }
.chaxis { font-size:9px; }
.legend { display:flex; gap:14px; font-size:11px; color:var(--muted);
  margin-bottom:6px; flex-wrap:wrap; }
.legend i { display:inline-block; width:10px; height:2.5px;
  vertical-align:middle; margin-right:4px; }
.wheels { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
.wheel { text-align:center; padding:8px 4px; border-radius:8px;
  background:rgba(128,128,128,.1); }
.wheel span { display:block; font-size:11px; color:var(--muted); }
.wheel b { font-size:calc(16px * var(--num-scale)); font-family:var(--mono); }
.wheel.hot b { color:var(--bad); } .wheel.cold b { color:var(--accent); }
.gforce { text-align:center; padding:8px; border-radius:8px;
  background:rgba(128,128,128,.1); }
.gforce b { font-size:calc(20px * var(--num-scale)); font-family:var(--mono); }
.warn { background:rgba(220,53,69,.1); border:1px solid var(--bad);
  color:var(--bad); border-radius:8px; padding:8px 12px;
  font-size:12px; margin-bottom:10px; }
/* 车辆状态提示（暂停/静止/加载）——避免把「暂停后的上报值」误认为卡死 */
.badge { border-radius:8px; padding:8px 12px; font-size:12.5px;
  margin-bottom:10px; font-weight:500; border:1px solid; }
/* 🔴 提示条要同时适配 9 套主题：写死深色只在浅色主题上看得清。
   color-mix 把「警示色」和当前主题的文字色混一下，深浅背景都自动可读；
   老浏览器不认 color-mix 就丢弃该行，保留上面那行兜底色（浅色场景正确）。 */
.badge.pause { background:rgba(255,193,7,.16); border-color:#ffc107;
  color:#8a6100; color:color-mix(in srgb, #ffc107 45%, var(--text)); }
.badge.idle  { background:rgba(var(--accent-rgb),.10); border-color:var(--accent);
  color:var(--accent); color:color-mix(in srgb, var(--accent) 65%, var(--text)); }
.empty { text-align:center; padding:32px; color:var(--muted); font-size:14px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th,td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); }
th { color:var(--muted); font-weight:500; }
</style>
</head>
<body>

<div class="bar">
  <span class="dot wait" id="dot"></span>
  <span class="stat" id="status">连接中…</span>
  <span class="stat">圈 <b id="lap">-</b></span>
  <span class="stat">本圈 <b id="laptime">-</b></span>
  <span class="stat">最佳 <b id="best">-</b></span>
  <span class="stat">采样 <b id="hz">-</b></span>
  <span class="stat">包格式 <b id="layout">-</b></span>
  <span class="stat" id="carWrap" style="display:none">车 <b id="carname">-</b></span>
  <span class="nav">
    <a href="/sessions">历史场次</a>
    <a href="#" id="prefLink" onclick="event.preventDefault(); togglePrefPanel(true)">个性化</a>
    <a href="#" id="layoutLink" onclick="event.preventDefault(); toggleLayoutPanel(true)">布局</a>
    <a href="#" id="glossLink" onclick="event.preventDefault(); toggleGlossary(true)">术语说明</a>
    <a href="#" id="apiLink" onclick="event.preventDefault(); toggleApiPanel(true)">API</a>
  </span>
</div>

<!-- 术语说明（点「术语说明」弹出，右侧滑入） -->
<div id="glossWrap" class="gloss-wrap">
  <div class="gloss-mask" onclick="toggleGlossary(false)"></div>
  <aside class="gloss" role="dialog" aria-label="术语说明">
    <header>
      <h3>术语说明</h3>
      <button onclick="toggleGlossary(false)" aria-label="关闭">✕</button>
    </header>
    <div class="gloss-body">
      <section>
        <h4>G 力球</h4>
        <p>中心的小球代表<b>此刻</b>车辆受到的总加速度：偏离中心越远，G 值越大。
           <b>向上</b>是加速，<b>向下</b>是刹车，左右对应转弯的离心方向，
           中心到球的连线表示受力方向。</p>
        <p class="dim">外圈参考环为 1g / 2g / 3g。球会平滑跟随，不会硬跳；
           数据中断 2 秒后自动回正。</p>
      </section>
      <section>
        <h4>G-G 图（抓地力圆）</h4>
        <p>把整场比赛中<b>每一帧</b>的（横向 G，纵向 G）打成散点。
           散点铺开的外边界近似等于轮胎能提供的合力极限——
           也就是常说的「抓地力圆」。</p>
        <p class="dim">点的颜色按合力大小着色（蓝→绿→橙→红）。
           越靠外圈说明当时轮胎越接近极限。散点是慢变量，看的是整场的驾驶风格；
           当前位置请对照左侧的 G 力球。</p>
      </section>
      <section>
        <h4>G 力矢量</h4>
        <p>横向与纵向 G 的合成大小（√(横向² + 纵向²)），
           即「总共承受了几个 g」。四轮卡片右下角显示的就是它。</p>
      </section>
      <section>
        <h4>G 值是什么单位</h4>
        <p>1g = 9.81 m/s²，即一个标准重力。1g 的横向 G 意味着
           轮胎正在以自身重量一倍的力把车推进弯里。
           民用车的极限约 0.8~1g，GT7 里的赛车普遍能到 2~3g。</p>
        <p class="dim">横向 G 带符号：<b>正值 = 右转</b>，负值 = 左转；
           纵向 G 正值 = 加速、负值 = 刹车。所以 G-G 图上左右两半
           分别对应左弯和右弯，正常跑一圈应该两边都有点。</p>
      </section>
      <section>
        <h4>行车轨迹的着色</h4>
        <p>轨迹按<b>每一点的 G 力大小</b>上色：
           <span class="k blue">蓝</span>=低速/直道 ·
           <span class="k green">绿</span>=中等 ·
           <span class="k orange">橙</span>=较高 ·
           <span class="k red">红</span>=极高。</p>
        <p class="dim">红色段通常就是重刹点或急弯。绿圈是起点，蓝白点是当前位置。</p>
      </section>
      <section>
        <h4>圈速</h4>
        <ul>
          <li><b>当前圈</b>：正在跑第几圈</li>
          <li><b>最快圈</b>：本场已跑出的最快单圈</li>
          <li><b>上一圈</b>：刚结束那一圈的成绩</li>
          <li><b>本场圈数</b>：这局比赛的总圈数</li>
        </ul>
        <p class="dim">时间格式为 分:秒.毫秒。GT7 未跑完一圈时会给出占位值，这里显示成 --:--.---</p>
      </section>
      <section>
        <h4>每圈圈速列表</h4>
        <p>逐圈记录本场每一圈的成绩，<b>新圈在最上面</b>，
           最快的一圈标绿并带 ★。GT7 原生只报「最快圈 / 上一圈」两个字段，
           完整列表是接收器检测到圈速变化后逐圈攒出来的。</p>
        <p class="dim">列表最多保留 60 圈；换新场次后自动清空重新开始记。</p>
      </section>
      <section>
        <h4>本场极速 与 近10秒极速</h4>
        <p><b>本场极速</b>是这一场里出现过的最高速度，只增不减；
           <b>近10秒极速</b>只统计最近 10 秒的窗口。</p>
        <p class="dim">两者不同的原因：刚跑完长直道后急刹，
           当前速度会掉下来，但本场极速仍然保持着直道尾速。</p>
      </section>
      <section>
        <h4>涡轮</h4>
        <p>涡轮增压的当前输出强度。数值越大，增压越高、动力越强。
           松开油门时会回落。</p>
      </section>
      <section>
        <h4>油量 / 电量</h4>
        <p>燃油车显示<b>剩余油量百分比</b>（GT7 的 gasLevel，低于 20% 进度条转红）；
           纯电车显示<b>剩余电量 kWh</b>（GT7 在纯电车时把 gasLevel 字段当成电量上报，
           油箱容量为 0，据此自动识别动力类型）。</p>
        <p class="dim">用于判断还能跑几圈或是否需要进站。</p>
      </section>
      <section>
        <h4>踏板</h4>
        <p>油门 / 刹车开度，0~100%。数值直接来自遥测上报的原始开度，
           不是根据速度推测的。</p>
      </section>
      <section>
        <h4>转速与档位</h4>
        <p>发动机转速（rpm）与当前档位。转速表的圆弧按 15000rpm 满量程绘制。
           档位旁偶有「建议档」提示，来自 GT7 的换档指引。</p>
      </section>
      <section>
        <h4>四轮温度</h4>
        <p>四条轮胎的表面温度。偏低（冷胎）抓地力不足，偏高（过热）同样会掉抓地力。
           本仪表把 &lt;68℃ 标为冷（蓝）、&gt;112℃ 标为过热（红），
           两者之间为正常工作区间。</p>
      </section>
      <section>
        <h4>场次是怎么划分的</h4>
        <p>一场 = 一次进入赛道到离开赛道。判定依据：</p>
        <ul>
          <li>连续多帧判定「在赛道上」→ 开始记录，单独存一个文件</li>
          <li>离开赛道（回到菜单/结算/换赛道）持续 15 秒 → 本场结束</li>
          <li>或圈数从较大值重置回 0/1（原地重开一局）→ 新场次</li>
        </ul>
        <p class="dim">所以「跑完一场回菜单再跑一场」会得到两条独立记录，
           在「历史场次」里可以分别查看。</p>
      </section>
      <section>
        <h4>顶部状态徽章（暂停 / 静止）</h4>
        <p>GT7 在<b>暂停菜单</b>里依然以 60Hz 上报遥测，但内容是
           「速度 0 / 怠速转速 / 踏板全 0」。界面识别到这种情况会显示
           「⏸ 游戏已暂停」徽章，避免被误认为数据卡死；
           车停在赛道上（怠速）则显示「🅿 车辆静止」。</p>
      </section>
      <section>
        <h4>采样计数</h4>
        <p>「采样」是本场实际落盘的帧数，「包格式 A/B」是收到的数据包数。
           两者不同是因为：加密包解密失败、PS5 混发的空壳包、
           暂停时的重复帧都会被过滤，<b>不会写入文件</b>。</p>
        <p class="dim">正常跑图时帧率约 60 帧/秒。如果「采样」长时间为 0
           而「包格式」在涨，说明车辆判定出了问题，把日志发给助手排查。</p>
      </section>
      <section>
        <h4>数据来源</h4>
        <p>全部数值来自 PS5 通过 UDP 广播的 GT7 官方遥测
           （Salsa20 加密，本地解密后解析），
           不经过任何第三方服务，也不会上传到云端。</p>
      </section>
    </div>
  </aside>
</div>

<!-- API 快速参考（点「API」弹出） -->
<div id="apiWrap" class="gloss-wrap">
  <div class="gloss-mask" onclick="toggleApiPanel(false)"></div>
  <aside class="gloss" role="dialog" aria-label="API 说明">
    <header>
      <h3>数据 API</h3>
      <button onclick="toggleApiPanel(false)" aria-label="关闭">✕</button>
    </header>
    <div class="gloss-body">
      <section>
        <h4>Base URL</h4>
        <p class="codebox">http://localhost:8787</p>
        <p class="dim">均为 GET（场次管理为 POST），返回 UTF-8 JSON，带 CORS——
           网页/脚本/直播覆盖层可直接消费，解密已在本地完成。</p>
      </section>
      <section>
        <h4>常用端点</h4>
        <ul class="apilist">
          <li><b>GET /api/v1/live?frames=N</b><br>
              <span class="dim">实时遥测：最新帧 + N 帧历史 + 轨迹 + G-G 散点</span></li>
          <li><b>GET /api/v1/laps</b><br>
              <span class="dim">每圈圈速与最快圈</span></li>
          <li><b>GET /api/v1/sessions</b><br>
              <span class="dim">历史场次列表（含收藏 / 自定义名）</span></li>
          <li><b>GET /api/v1/sessions/&lt;文件名&gt;</b><br>
              <span class="dim">单场统计摘要</span></li>
          <li><b>GET /api/v1/sessions/&lt;文件名&gt;/download</b><br>
              <span class="dim">下载原始 jsonl 数据文件</span></li>
          <li><b>POST /api/v1/sessions/&lt;文件名&gt;/{favorite,rename,delete}</b><br>
              <span class="dim">场次管理：收藏 / 改名 / 删除</span></li>
        </ul>
      </section>
      <section>
        <h4>示例</h4>
        <p class="codebox">curl "http://localhost:8787/api/v1/live?frames=60"</p>
        <p class="dim">完整字段与单位说明：
           <a href="/api/v1/docs" target="_blank" style="color:var(--accent)">/api/v1/docs</a>
           （text/markdown）</p>
      </section>
    </div>
  </aside>
</div>

<!-- 布局设置（点「布局」弹出） -->
<div id="layoutWrap" class="gloss-wrap">
  <div class="gloss-mask" onclick="toggleLayoutPanel(false)"></div>
  <aside class="gloss" role="dialog" aria-label="布局设置">
    <header>
      <h3>布局设置</h3>
      <button onclick="toggleLayoutPanel(false)" aria-label="关闭">✕</button>
    </header>
    <div class="gloss-body">
      <section>
        <h4>选择布局</h4>
        <select id="layoutSel" onchange="switchLayout(this.value)"></select>
        <div class="rowbtns">
          <button onclick="resetLayout()">恢复默认排列</button>
          <button id="btnDelLayout" onclick="deleteLayout()">删除此布局</button>
        </div>
        <input id="layoutName" placeholder="输入名称，把当前布局另存为…">
        <div class="rowbtns">
          <button onclick="saveLayoutAs()">保存为自定义布局</button>
        </div>
      </section>
      <section>
        <h4>卡片</h4>
        <div id="cardToggles"></div>
        <p class="dim">勾选控制显示；「整行」独占一行，「半宽」与另一张卡并排。
           <b>本面板打开期间，直接拖动卡片标题即可排序。</b></p>
      </section>
      <section>
        <button onclick="toggleLayoutPanel(false)"
          style="width:100%;border:1px solid var(--line);background:var(--card);
                 color:inherit;padding:9px;border-radius:7px;cursor:pointer;
                 font-family:inherit;font-size:13px">完成</button>
      </section>
    </div>
  </aside>
</div>

<!-- 个性化设置（点「个性化」弹出，右侧滑入） -->
<div id="prefWrap" class="gloss-wrap">
  <div class="gloss-mask" onclick="togglePrefPanel(false)"></div>
  <aside class="gloss" role="dialog" aria-label="个性化设置">
    <header>
      <h3>个性化设置</h3>
      <button onclick="togglePrefPanel(false)" aria-label="关闭">✕</button>
    </header>
    <div class="gloss-body">
      <section>
        <h4>主题预设</h4>
        <div class="themepick" id="themePick"></div>
        <p class="dim">「跟随系统」会随浏览器/系统的深浅色自动切换；其余预设固定不变。</p>
      </section>
      <section>
        <h4>强调色</h4>
        <div class="accentpick" id="accentPick"></div>
        <div class="rowbtns">
          <button onclick="setAccent('')">跟随主题</button>
          <input type="color" id="accentCustom" value="#0d6efd"
            onchange="setAccent(this.value)" aria-label="自定义强调色">
        </div>
        <p class="dim">影响导航高亮、转速环、进度条与链接色。选「跟随主题」则用当前预设自带的颜色。</p>
      </section>
      <section>
        <h4>排版</h4>
        <div class="trow">
          <label for="densitySel">卡片间距</label>
          <select id="densitySel" onchange="setDensity(this.value)">
            <option value="compact">紧凑</option>
            <option value="normal" selected>标准</option>
            <option value="cozy">宽松</option>
          </select>
        </div>
        <div class="trow" style="display:block">
          <label for="fontScale">读数大小 <output id="fontScaleOut">100%</output></label>
          <input type="range" id="fontScale" min="80" max="160" step="5" value="100"
            oninput="setFontScale(this.value)">
        </div>
        <p class="dim">「读数大小」只缩放速度、转速、圈速、胎温等数字，副屏远距离看更清楚。</p>
      </section>
      <section>
        <h4>单位</h4>
        <div class="trow">
          <label for="speedUnitSel">速度单位</label>
          <select id="speedUnitSel" onchange="setSpeedUnit(this.value)">
            <option value="kph" selected>km/h（公里每小时）</option>
            <option value="mph">mph（英里每小时）</option>
          </select>
        </div>
        <p class="dim">实时曲线与「本场极速」会同步换算。</p>
      </section>
      <section>
        <h4>图表</h4>
        <div class="trow">
          <label for="chartWinSel">实时曲线窗口</label>
          <select id="chartWinSel" onchange="setChartWindow(this.value)">
            <option value="120">约 2 秒</option>
            <option value="300" selected>约 5 秒</option>
            <option value="600">约 10 秒</option>
          </select>
        </div>
        <div class="trow">
          <label for="ggMaxSel">G 力量程</label>
          <select id="ggMaxSel" onchange="setGgMax(this.value)">
            <option value="1.5">±1.5 g</option>
            <option value="2">±2 g</option>
            <option value="3" selected>±3 g</option>
            <option value="4">±4 g</option>
          </select>
        </div>
        <div id="chartSeries"></div>
        <p class="dim">G 力量程同时作用于抓地力图与 G 力球的外圈参考环。</p>
      </section>
      <section>
        <h4>备份与恢复</h4>
        <textarea id="prefJson" class="prefjson" spellcheck="false"
          aria-label="偏好 JSON"></textarea>
        <div class="rowbtns">
          <button onclick="exportPrefs()">导出到此框</button>
          <button onclick="importPrefs()">从框内导入</button>
        </div>
        <div class="rowbtns">
          <button onclick="resetPrefs()">恢复全部默认</button>
        </div>
        <p class="dim">偏好存在<b>本浏览器</b>（localStorage），换设备/换浏览器不通用；
          想在两台机器间搬偏好，用上面的导出／导入。</p>
      </section>
      <section>
        <button onclick="togglePrefPanel(false)"
          style="width:100%;border:1px solid var(--line);background:var(--card);
                 color:inherit;padding:9px;border-radius:7px;cursor:pointer;
                 font-family:inherit;font-size:13px">完成</button>
      </section>
    </div>
  </aside>
</div>

<div id="warn" class="warn" style="display:none"></div>
<div id="statebadge" class="badge" style="display:none"></div>

<div id="main" style="display:none">
  <div id="cardGrid" class="cgrid">
    <div class="card" id="c-rpm">
      <h2>转速与速度</h2>
      <div class="dials">
        <div class="gauge">
          <svg width="132" height="132" viewBox="0 0 132 132">
            <circle class="bg-ring" cx="66" cy="66" r="54" fill="none"
              stroke-width="9"
              stroke-dasharray="254.5 339.3" stroke-linecap="round"
              transform="rotate(0 66 66)" />
            <circle id="rpmArc" cx="66" cy="66" r="54" fill="none"
              stroke-width="9" stroke-dasharray="0 339.3"
              stroke-linecap="round" />
          </svg>
          <div class="gauge-val">
            <b id="rpm">0</b><span id="gear">N 档</span>
            <span id="gaugeMax" style="font-size:10px;color:var(--muted)"></span>
          </div>
        </div>
        <div class="speed">
          <b id="speed">0</b><span id="speedUnit">km/h</span>
          <div style="margin-top:10px;font-size:11px;color:var(--muted)">
            本场极速 <b id="maxspeed" style="font-family:var(--mono)">-</b>
          </div>
          <div style="margin-top:4px;font-size:11px;color:var(--muted)">
            近10秒 <b id="maxspeed10" style="font-family:var(--mono)">-</b>
          </div>
        </div>
      </div>
      <div class="shiftlamp" id="shiftLamp">
        <i></i><i></i><i></i><i></i><i></i><i></i><i></i>
      </div>
      <div style="text-align:center;font-size:11px;color:var(--muted)">
        建议档 <b id="sugGear" style="font-family:var(--mono)">-</b>
        · 换挡区间 <b id="alertRpm" style="font-family:var(--mono)">-</b>
      </div>
    </div>

    <div class="card" id="c-pedal">
      <h2>踏板与 G 力</h2>
      <div class="bar-row"><label>油门</label>
        <div class="track"><div class="fill t" id="fThr" style="width:0"></div></div>
        <output id="oThr">0%</output></div>
      <div class="bar-row"><label>刹车</label>
        <div class="track"><div class="fill b" id="fBrk" style="width:0"></div></div>
        <output id="oBrk">0%</output></div>
      <div class="bar-row"><label>横向 G</label>
        <div class="track"><div class="fill lat" id="fLat" style="width:0"></div></div>
        <output id="oLat">0.00</output></div>
      <div class="bar-row"><label>纵向 G</label>
        <div class="track"><div class="fill lon" id="fLon" style="width:0"></div></div>
        <output id="oLon">0.00</output></div>
    </div>

    <div class="card" id="c-lap">
      <h2 id="lapCardTitle">圈速与油量</h2>
      <div class="laps">
        <div><span>当前圈</span><b id="lapNo">-</b></div>
        <div><span>最快圈</span><b id="bestLap">--:--.---</b></div>
        <div><span>上一圈</span><b id="lastLap">--:--.---</b></div>
        <div><span>本场圈数</span><b id="lapsInRace">-</b></div>
      </div>
      <div class="bar-row" style="margin-top:12px"><label id="fuelLabel">油量</label>
        <div class="track"><div class="fill fuel" id="fFuel" style="width:0"></div></div>
        <output id="oFuel">--</output></div>
      <div class="bar-row"><label>涡轮</label>
        <div class="track"><div class="fill turbo" id="fTurbo" style="width:0"></div></div>
        <output id="oTurbo">0.00</output></div>
      <div class="laplist" id="lapList"></div>
      <div id="fuelStrategy" style="margin-top:9px;font-size:12.5px;
        color:var(--muted)"></div>
    </div>

    <div class="card" id="c-gball">
      <h2>G 力球 <span id="ggJudge" style="float:right;font-weight:400"></span></h2>
      <canvas id="gball" width="680" height="620"
        style="width:100%;max-width:380px;aspect-ratio:680/620;display:block;margin:0 auto"></canvas>
      <div class="ggreadout">
        <span>横向<b id="ggLat">0.00</b></span>
        <span>纵向<b id="ggLon">0.00</b></span>
        <span>合力<b id="ggTotal">0.00</b></span>
      </div>
    </div>

    <div class="card" id="c-gg">
      <h2>G-G 图（抓地力圆）</h2>
      <canvas id="gg" width="680" height="620"
        style="width:100%;max-width:380px;aspect-ratio:680/620;display:block;margin:0 auto"></canvas>
      <div class="legend" style="justify-content:center;margin-top:8px">
        <span>整场散点 · 圆环 = 1g / 2g / 3g</span>
      </div>
    </div>

    <div class="card span2" id="c-map">
    <h2>行车轨迹 <span id="mapinfo" style="float:right;font-weight:400"></span></h2>
    <canvas id="map" width="760" height="420"
      style="width:100%;max-width:760px;display:block;margin:0 auto"></canvas>
    <div class="legend" style="justify-content:center;margin-top:6px">
      <span><i style="background:#0d6efd"></i>低 G</span>
      <span><i style="background:#198754"></i>中 G</span>
      <span><i style="background:#fd7e14"></i>高 G</span>
      <span><i style="background:#dc3545"></i>极高 G（重刹/急弯）</span>
    </div>
  </div>

    <div class="card span2" id="c-chart">
    <h2>实时曲线 <span id="range" style="float:right;font-weight:400"></span></h2>
    <div id="chart"></div>
  </div>

    <div class="card span2" id="c-wheel">
    <h2>四轮状态</h2>
    <div class="wheels">
      <div class="wheel" id="w0"><span>左前</span><b>--</b></div>
      <div class="wheel" id="w1"><span>右前</span><b>--</b></div>
      <div class="wheel" id="w2"><span>左后</span><b>--</b></div>
      <div class="wheel" id="w3"><span>右后</span><b>--</b></div>
      <div class="gforce"><span>G 力矢量</span><b id="gvec">0.00</b></div>
    </div>
    <div class="susp" id="suspRow">
      <span>悬挂 左前 <b id="s0" style="font-family:var(--mono)">--</b></span>
      <span>右前 <b id="s1" style="font-family:var(--mono)">--</b></span>
      <span>左后 <b id="s2" style="font-family:var(--mono)">--</b></span>
      <span>右后 <b id="s3" style="font-family:var(--mono)">--</b></span>
    </div>
    </div>

    <div class="card" id="c-engine">
      <h2 id="engineTitle">引擎健康</h2>
      <div class="eng-row"><span>水温</span><b id="eWater">--</b></div>
      <div class="eng-row" id="eOilRow"><span>油温</span><b id="eOil">--</b></div>
      <div class="eng-row" id="eOilPRow"><span>油压</span><b id="eOilP">--</b></div>
      <div class="eng-row"><span>车身高度</span><b id="eBody">--</b></div>
      <div class="eng-row" id="eRecRow" style="display:none"><span>能量回收</span><b id="eRec">--</b></div>
    </div>

    <div class="card" id="c-race">
      <h2>比赛信息</h2>
      <div class="eng-row"><span>赛道时间</span><b id="rClock">--</b></div>
      <div class="eng-row"><span>发车位</span><b id="rGrid">--</b></div>
      <div class="eng-row"><span>当前名次</span><b id="rPos">--</b></div>
      <div class="eng-row"><span>参赛车辆</span><b id="rCars">--</b></div>
      <div class="eng-row"><span>比赛状态</span><b id="rState">--</b></div>
    </div>
  </div>
</div>

<div id="empty" class="card empty">
  等待 GT7 遥测数据…<br>
  <span style="font-size:12px">
    请确认 PS5 已开机并进入赛道，且与本机同一网段
  </span>
</div>

<script>
const $ = id => document.getElementById(id);
const RPM_MAX = 15000, SPEED_MAX = 320;
let bestLap = null, lastFrame = null;
// —— 转速表自适应量程的状态（一场内累计观测，换车重置） ——
let rpmSeenMax = 0;        // 本场见过的最高转速
let rpmSeenCar = '';       // 观测时的车型名（换车即重置观测）
let rpmGaugeMax = RPM_MAX; // 当前表底
let pauseHoldUntil = 0;    // 暂停徽标的粘滞截止时间（见 render 内说明）
// 把任意值向上取到「好看的表底」：7500 / 8000 / 26000 这种，而不是 7312。
function niceCeil(x) {
  const step = x > 12000 ? 1000 : (x > 6000 ? 500 : 250);
  return Math.ceil(x / step) * step;
}

// ===========================================================================
// 个性化设置：主题预设 / 强调色 / 排版 / 单位 / 图表
// ===========================================================================
// 存 localStorage（每浏览器一份），理由同布局偏好：这些都是视觉习惯，
// 跟设备和屏幕尺寸强相关，也不必为它给服务端加写接口。
// 🔴 新增主题要改两处：这里的 THEMES + CSS 里的 [data-theme="xxx"]，靠 id 对齐。
//    只改一侧的表现是「面板里有这个选项，点了没反应」。
const PREF_KEY = 'gt7_prefs_v1';
const THEMES = [
  {id:'auto',     name:'跟随系统', desc:'随系统深浅色自动切换',
   c:['#f5f6f7','#16181c','#0d6efd']},
  {id:'light',    name:'浅色', desc:'默认白底，白天清晰',
   c:['#f5f6f7','#fff','#0d6efd']},
  {id:'dark',     name:'深色', desc:'夜里不晃眼',
   c:['#16181c','#1e2126','#2b7fff']},
  {id:'oled',     name:'OLED 赛道黑', desc:'纯黑底＋青柠，夜间/OLED 屏',
   c:['#000','#0a0b0d','#00e0c6']},
  {id:'sepia',    name:'米黄护眼', desc:'暖色纸质底，长时间看数据',
   c:['#f2e9d7','#fdf8ec','#b45309']},
  {id:'racing',   name:'GT 竞速', desc:'碳纤黑＋GT 红，热血赛道向',
   c:['#0d0d0f','#16161a','#e10600']},
  {id:'glacier',  name:'冰川蓝', desc:'冷调浅色，清爽轻量',
   c:['#eaf2fb','#fff','#0a84ff']},
  {id:'hud',      name:'HUD 荧光', desc:'黑底荧光绿，赛博 HUD 观感',
   c:['#04100a','#08180f','#39ff14']},
  {id:'midnight', name:'午夜紫', desc:'暗紫长夜复盘向',
   c:['#0c0a18','#15122a','#9b6bff']},
];
const ACCENTS = ['#0d6efd','#0a84ff','#00e0c6','#198754','#b45309',
                 '#e10600','#e83e8c','#9b6bff','#39ff14','#ffb020'];
// 曲线条目 = 分面顺序。颜色用 Okabe-Ito 色弱安全色板（固定色值，
// 不随主题变）：四条线原本共用一套红绿橙蓝，转速红和刹车橙太接近，
// 分面后每条线有独立小图，颜色只起辅助标识作用。
const SERIES_META = [
  {k:'speed',    label:'速度', unit:'km/h', c:'#0072B2'},
  {k:'rpm',      label:'转速', unit:'rpm',  c:'#D55E00'},
  {k:'throttle', label:'油门', unit:'%',    c:'#009E73'},
  {k:'brake',    label:'刹车', unit:'%',    c:'#CC79A7'},
];
const WINDOWS = [120, 300, 600];        // ≈2/5/10 秒（按 60Hz 采样折算）
const DEFAULT_PREFS = {
  theme: 'auto', accent: '', density: 'normal', fontScale: 100,
  speedUnit: 'kph', chartWindow: 300, ggMax: 3,
  chartSeries: {speed:true, rpm:true, throttle:true, brake:true},
};

function hexToRgbStr(hex) {
  let h = String(hex || '').replace('#', '');
  if (h.length === 3) h = h[0]+h[0]+h[1]+h[1]+h[2]+h[2];
  const n = parseInt(h, 16);
  if (isNaN(n) || h.length !== 6) return '';
  return ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255);
}
function loadPrefs() {
  const p = JSON.parse(JSON.stringify(DEFAULT_PREFS));   // 深拷贝，别改到默认对象
  try {
    const s = JSON.parse(localStorage.getItem(PREF_KEY) || '{}');
    Object.assign(p, s);
    // 嵌套对象要单独浅合并：直接 Object.assign 会整个覆盖，缺字段就没了
    if (s.chartSeries) Object.assign(p.chartSeries, s.chartSeries);
  } catch (e) { /* 存档坏了就从头来 */ }
  // 合法性兜底：用户手改过 localStorage、或旧版本存的值现在不认了
  if (!THEMES.some(t => t.id === p.theme)) p.theme = 'auto';
  if (['compact','normal','cozy'].indexOf(p.density) < 0) p.density = 'normal';
  if (['kph','mph'].indexOf(p.speedUnit) < 0) p.speedUnit = 'kph';
  if (WINDOWS.indexOf(+p.chartWindow) < 0) p.chartWindow = 300;
  p.fontScale = Math.min(160, Math.max(80, +p.fontScale || 100));
  p.ggMax = Math.min(4, Math.max(1.5, +p.ggMax || 3));
  return p;
}
let prefs = loadPrefs();
function savePrefs() {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); }
  catch (e) { /* 隐私模式写不进去：本次会话内仍生效 */ }
}

// canvas 不继承 CSS 变量，画之前把变量值取出来交给 ctx
function gvar(name, fb) {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim();
  return v || fb || '#888';
}
// 速度换算：全 UI 只此一处知道 mph 的存在，其余照常接收 km/h
const KPH_TO_MPH = 0.621371;
function spd(kph) { return prefs.speedUnit === 'mph' ? kph * KPH_TO_MPH : kph; }
function spdUnit() { return prefs.speedUnit === 'mph' ? 'mph' : 'km/h'; }

function applyPrefs() {
  const d = document.documentElement;
  d.setAttribute('data-theme', prefs.theme);
  d.setAttribute('data-density', prefs.density);
  d.style.setProperty('--num-scale', (prefs.fontScale / 100).toFixed(2));
  const rgb = hexToRgbStr(prefs.accent);
  if (rgb) {
    d.style.setProperty('--accent', prefs.accent);
    d.style.setProperty('--accent-rgb', rgb);
  } else {
    // 没选自定强调色 → 清掉内联值，让当前主题预设自带的颜色生效
    d.style.removeProperty('--accent');
    d.style.removeProperty('--accent-rgb');
  }
  $('speedUnit').textContent = spdUnit();
  GG_MAX = +prefs.ggMax;          // 抓地力图与 G 力球共用同一量程
  renderPrefPanel();
}

function setTheme(id)     { prefs.theme = id; savePrefs(); applyPrefs(); }
function setAccent(v)     { prefs.accent = v || ''; savePrefs(); applyPrefs(); }
function setDensity(v)    { prefs.density = v; savePrefs(); applyPrefs(); }
function setSpeedUnit(v)  { prefs.speedUnit = v; savePrefs(); applyPrefs(); }
function setGgMax(v)      { prefs.ggMax = +v; savePrefs(); applyPrefs(); }
function setChartWindow(v){ prefs.chartWindow = +v; savePrefs(); applyPrefs(); }
function setFontScale(v) {
  prefs.fontScale = +v; savePrefs();
  document.documentElement.style.setProperty(
    '--num-scale', (prefs.fontScale / 100).toFixed(2));
  $('fontScaleOut').textContent = prefs.fontScale + '%';
}
function toggleSeries(k, on) { prefs.chartSeries[k] = !!on; savePrefs(); }

function renderPrefPanel() {
  $('themePick').innerHTML = THEMES.map(t =>
    '<button class="tbox' + (t.id === prefs.theme ? ' on' : '') +
    '" onclick="setTheme(\'' + t.id + '\')"><span class="sw">' +
    t.c.map(c => '<i style="background:' + c + '"></i>').join('') +
    '</span><b>' + t.name + '</b><span>' + t.desc + '</span></button>').join('');
  $('accentPick').innerHTML = ACCENTS.map(a =>
    '<button style="background:' + a + '"' + (prefs.accent === a ? ' class="on"' : '') +
    ' title="' + a + '" aria-label="强调色 ' + a +
    '" onclick="setAccent(\'' + a + '\')"></button>').join('');
  $('chartSeries').innerHTML = SERIES_META.map(s =>
    '<div class="trow"><label><input type="checkbox"' +
    (prefs.chartSeries[s.k] ? ' checked' : '') +
    ' onchange="toggleSeries(\'' + s.k + '\',this.checked)"> ' + s.label +
    '</label><span style="width:22px;height:3px;border-radius:2px;background:' +
    s.c + '"></span></div>').join('');
  // 面板每次打开都按当前偏好回填控件（手改 localStorage 或导入后也要同步）
  $('densitySel').value = prefs.density;
  $('speedUnitSel').value = prefs.speedUnit;
  $('chartWinSel').value = String(prefs.chartWindow);
  $('ggMaxSel').value = String(prefs.ggMax);
  $('fontScale').value = prefs.fontScale;
  $('fontScaleOut').textContent = prefs.fontScale + '%';
  $('accentCustom').value = hexToRgbStr(prefs.accent)
    ? prefs.accent : gvar('--accent', '#0d6efd');
}
function togglePrefPanel(open) {
  $('prefWrap').classList.toggle('open', !!open);
  if (open) { renderPrefPanel(); exportPrefs(); }
}
function exportPrefs() {
  $('prefJson').value = JSON.stringify(prefs, null, 1);
}
function importPrefs() {
  let v = ($('prefJson').value || '').trim();
  try {
    const s = JSON.parse(v);
    if (!s || typeof s !== 'object' || Array.isArray(s)) throw new Error('格式不符');
    localStorage.setItem(PREF_KEY, JSON.stringify(s));
    prefs = loadPrefs();      // 走一遍兜底校验，脏值不会被写进状态
    applyPrefs();
    v = '已导入并应用（非法字段已自动纠正为默认值）';
  } catch (e) {
    v = '导入失败：' + e.message + '。请把导出的 JSON 原样粘贴到这里。';
  }
  $('prefJson').value = v;
}
function resetPrefs() {
  if (!confirm('恢复默认主题、强调色、单位与图表设置？（已保存的布局不受影响）')) return;
  try { localStorage.removeItem(PREF_KEY); } catch (e) { /* 读不到就算了 */ }
  prefs = loadPrefs();
  applyPrefs();
  exportPrefs();
}

function fmtLap(sec) {
  if (!sec && sec !== 0) return '-';
  const m = Math.floor(sec / 60), s = sec - m * 60;
  return m + ':' + s.toFixed(3).padStart(6, '0');
}

function render(s) {
  const L = s.latest;
  if (!L) return;

  // 状态栏
  // 🔴 connected 由服务端按「状态文件 5 秒内有无更新」判定，
  //    PS5 关机/退游戏后旧文件还在，必须靠服务端判过期。
  const dot = $('dot');
  if (s.connected) { dot.className = 'dot on'; $('status').textContent = '采集中'; }
  else { dot.className = 'dot off'; $('status').textContent = '已断开（PS5 未开机或已退出游戏）'; }
  // 菜单态（lap=65535）圈号/圈速都是无意义值，显示占位
  const inMenu = L.lap == null || L.lap >= 65000;
  $('lap').textContent = inMenu ? '--' : L.lap;
  $('laptime').textContent = (!s.connected || inMenu) ? '--:--.---' : fmtLap(s.lap_time);
  $('hz').textContent = s.frames;
  const lay = s.layouts || {};
  $('layout').textContent = Object.keys(lay).map(k => k + ':' + lay[k]).join(' ');
  if (s.car_name) { $('carname').textContent = s.car_name;
    $('carWrap').style.display = ''; }

  if (s.warning) {
    const w = $('warn'); w.style.display = 'block';
    w.textContent = '⚠ ' + s.warning;
  }

  // —— 车辆状态提示 ——
  // 🔴 GT7 暂停时会继续以 60Hz 上报「速度0 / 转速怠速 / 踏板0」，
  //    如果界面不说明，很容易被误认为「数据卡死」。
  //    这里明确告诉用户当前是什么状态。
  const sb = $('statebadge');
  // 🔴 暂停判定加 1.5s 粘滞：GT7 的 Paused 位（flags bit1）在部分暂停
  //    场景（暂停菜单切换瞬间 / 回放 / 换镜头）会短暂清零，直接跟随
  //    就出现「有时显示有时不显示」。见到暂停位就保持 1.5s；
  //    期间车速起来（真的继续跑了）则立刻解除。
  if (L.paused) pauseHoldUntil = Date.now() + 1500;
  const showPaused = Date.now() < pauseHoldUntil && (L.paused || (L.speed_kph || 0) < 1);
  if (showPaused) {
    sb.className = 'badge pause';
    sb.textContent = '⏸ 游戏已暂停 —— 以下数值是 GT7 暂停时的上报值（速度0/踏板0 属正常）';
    sb.style.display = 'block';
  } else if (L.loading) {
    sb.className = 'badge pause';
    sb.textContent = '⏳ 场景加载中…';
    sb.style.display = 'block';
  } else if (L.speed_kph < 1 && L.rpm < 1500 && L.throttle < 0.02 && L.brake < 0.02) {
    sb.className = 'badge idle';
    sb.textContent = '🅿 车辆静止（怠速）';
    sb.style.display = 'block';
  } else {
    sb.style.display = 'none';
  }

  $('empty').style.display = 'none';
  $('main').style.display = 'block';

  // 转速表
  // 🔴 量程必须按车自适应：固定 15000 的后果是「游戏里表满、这里才 2/3」
  //    （家用车红区 7k 上），电车电机 2 万+ 又会把表撑爆。
  //    优先用游戏下发的红区 max_alert_rpm；没给就用本场见过的最高转速
  //    放大 6%；换车（车型名变化）后重新观测。
  rpmSeenMax = Math.max(rpmSeenMax, L.rpm || 0);
  if (s.car_name && rpmSeenCar && s.car_name !== rpmSeenCar) rpmSeenMax = 0;
  rpmSeenCar = s.car_name || rpmSeenCar;
  const aMin0 = L.min_alert_rpm || 0, aMax0 = L.max_alert_rpm || 0;
  const baseMax = aMax0 > 0 ? aMax0 * 1.06
                : (rpmSeenMax > 0 ? rpmSeenMax * 1.06 : RPM_MAX);
  rpmGaugeMax = niceCeil(Math.max(baseMax, 3000));
  const rpm = L.rpm || 0;
  const frac = Math.min(rpm / rpmGaugeMax, 1);
  $('rpm').textContent = Math.round(rpm);
  $('gear').textContent = (L.gear > 0 ? L.gear : (L.gear === 0 ? 'N' : 'R')) + ' 档';
  $('rpmArc').setAttribute('stroke-dasharray', (frac * 254.5) + ' 339.3');
  $('gaugeMax').textContent = '表底 ' + Math.round(rpmGaugeMax).toLocaleString('en-US');

  // 速度（显示单位由个性化面板决定，内部一律 km/h）
  $('speed').textContent = Math.round(spd(L.speed_kph));
  // 本场极速：由接收器按会话累计（不随历史缓冲滚动而变）
  if (s.session_max_speed != null) {
    $('maxspeed').textContent = Math.round(spd(s.session_max_speed));
  }
  // 近 10 秒极速：从 history 窗口算，用于对照「刚才跑多快」
  let mx10 = 0;
  for (const f of s.history) if (f.speed_kph > mx10) mx10 = f.speed_kph;
  $('maxspeed10').textContent = Math.round(spd(mx10));

  // 踏板
  const thr = Math.round(L.throttle * 100), brk = Math.round(L.brake * 100);
  $('fThr').style.width = thr + '%'; $('oThr').textContent = thr + '%';
  $('fBrk').style.width = brk + '%'; $('oBrk').textContent = brk + '%';

  // G 力：转成百分比显示（±2g 满量程）
  const lat = (L.g_force && L.g_force[1]) || 0;
  const lon = (L.g_force && L.g_force[0]) || 0;
  $('fLat').style.width = Math.min(100, Math.abs(lat) / 2 * 100) + '%';
  $('oLat').textContent = lat.toFixed(2);
  $('fLon').style.width = Math.min(100, Math.abs(lon) / 2 * 100) + '%';
  $('oLon').textContent = lon.toFixed(2);

  // 四轮温度：过冷/过热变色
  const names = ['w0','w1','w2','w3'];
  (L.tyre_temp || []).forEach((t, i) => {
    if (!names[i]) return;
    const el = $(names[i]);
    el.querySelector('b').textContent = Math.round(t) + '°';
    el.className = 'wheel' + (t > 112 ? ' hot' : (t < 68 ? ' cold' : ''));
  });
  $('gvec').textContent = Math.hypot(lat, lon).toFixed(2);

  // 悬挂高度（米 → 厘米显示；GT7 该值是相对基准的行程，可为负）
  (L.susp_height || []).forEach((h, i) => {
    const el = $('s' + i); if (!el) return;
    el.textContent = (h === 0 && !L.has_coords) ? '--' : (h * 100).toFixed(1) + 'cm';
  });
  $('suspRow').style.display = (L.susp_height && L.susp_height.some(v => v !== 0))
    ? '' : 'none';

  // —— 换挡灯：min/maxAlertRPM 给出换挡窗口，转速逼近上限时点亮 ——
  const aMin = L.min_alert_rpm || 0, aMax = L.max_alert_rpm || 0;
  const lamp = $('shiftLamp');
  if (aMax > 0) {
    const r0 = L.rpm || 0;
    // 7 段：从 aMin 到 aMax 分 7 档，超过 aMax 全亮闪烁
    let lit = 0;
    if (r0 >= aMax) lit = 7;
    else if (r0 > aMin) lit = Math.min(6, Math.floor((r0 - aMin) / (aMax - aMin) * 6) + 1);
    lamp.classList.toggle('hot', r0 >= aMax);
    Array.from(lamp.children).forEach((el, i) => el.classList.toggle('on', i < lit));
    $('alertRpm').textContent = Math.round(aMin) + '-' + Math.round(aMax);
  } else {
    // 无换挡数据 → 按自适应表底兜底（85% 起逐段点亮）
    const lit = Math.min(7, Math.floor((rpm / rpmGaugeMax) * 8));
    lamp.classList.toggle('hot', rpm >= rpmGaugeMax * 0.95);
    Array.from(lamp.children).forEach((el, i) => el.classList.toggle('on', i < lit));
    $('alertRpm').textContent = '-';
  }
  const sg = L.suggested_gear || 0;
  // 🔴 协议约定：建议档 15 = 「当前没有建议档」（电车/滑行时恒为 15），
  //    直接显示「建议档 15」是错的，要显示 '-'。
  $('sugGear').textContent = (sg > 0 && sg < 15) ? sg : '-';

  // —— 引擎健康 ——
  // 🔴 电车没有机油：油温/油压行必须隐藏，否则给电车显示「油压 0.0 bar
  //    过低报警」纯属误导。动力类型在下面油量段也会用到，这里先算一次。
  const isEVPwr = s.powertrain === 'electric';
  $('engineTitle').textContent = isEVPwr ? '动力系统' : '引擎健康';
  $('eOilRow').style.display = isEVPwr ? 'none' : '';
  $('eOilPRow').style.display = isEVPwr ? 'none' : '';
  const water = L.water_temp || 0, oilT = L.oil_temp || 0, oilP = L.oil_pressure || 0;
  const eW = $('eWater'), eO = $('eOil'), eP = $('eOilP');
  eW.textContent = water ? Math.round(water) + ' °C' : '--';
  eW.className = water > 105 ? 'hot' : (water && water < 60 ? '' : 'ok');
  eO.textContent = oilT ? Math.round(oilT) + ' °C' : '--';
  eO.className = oilT > 130 ? 'hot' : (oilT && oilT < 70 ? '' : 'ok');
  eP.textContent = oilP ? oilP.toFixed(1) + ' bar' : '--';
  eP.className = (oilP && oilP < 2.0) ? 'hot' : 'ok';
  $('eBody').textContent = L.body_height ? (L.body_height * 100).toFixed(1) + ' cm' : '--';
  // 能量回收：仅扩展包(~)有心跳请求时才有值。有值才显示该行，避免占位。
  const eRec = (typeof L.energy_recovery === 'number' && L.energy_recovery !== 0)
    ? L.energy_recovery : null;
  $('eRecRow').style.display = eRec === null ? 'none' : '';
  // 瞬时回收常回落到 0，把本场峰值一并显示更有信息量
  const maxRec = s.max_energy_recovery || 0;
  $('eRec').textContent = eRec === null ? '--'
    : eRec.toFixed(1) + ' kW' + (maxRec > 0 ? '（峰值 ' + maxRec.toFixed(1) + '）' : '');

  // —— 比赛信息 ——
  const tod = L.time_of_day || 0;
  if (tod > 0) {
    const tot = Math.floor(tod / 1000);            // 当天已过秒数
    const hh = String(Math.floor(tot / 3600) % 24).padStart(2, '0');
    const mm = String(Math.floor(tot / 60) % 60).padStart(2, '0');
    const ss = String(tot % 60).padStart(2, '0');
    $('rClock').textContent = hh + ':' + mm + ':' + ss;
  } else { $('rClock').textContent = '--'; }
  // —— 比赛信息 ——
  // 🔴 0x84(quali_pos) 在比赛中是当前名次；真正的发车位是接收器在
  //    开跑瞬间快照的 grid_start。两者都可能是 65535(菜单态)/0，显示占位。
  const okPos = v => (v > 0 && v < 65000) ? v : 0;
  const gp = okPos(s.grid_start), cp = okPos(L.quali_pos);
  $('rGrid').textContent = gp ? ('第 ' + gp + ' 位') : '--';
  $('rPos').textContent = cp ? ('第 ' + cp + ' 位') : '--';
  $('rCars').textContent = okPos(L.num_cars) ? (L.num_cars + ' 辆') : '--';
  $('rState').textContent = L.paused ? '暂停' : (L.loading ? '加载中'
    : (L.car_on_track ? '在赛道' : '维修区/菜单'));

  // —— 圈速与油量 / 电量 ——
  $('lapNo').textContent = (L.lap != null && L.lap >= 0 && L.lap < 65000)
    ? L.lap : '-';
  $('bestLap').textContent = fmtMs(L.best_lap_ms);
  $('lastLap').textContent = fmtMs(L.last_lap_ms);
  $('lapsInRace').textContent = (L.laps_in_race != null && L.laps_in_race > 0)
    ? L.laps_in_race : '-';

  // 🔴 动力类型决定「能量」语义：
  //   · fuel / kart：gas_level = 油量百分比（0~100），gas_capacity = 容量
  //   · electric：gas_capacity == 0，**gas_level = 剩余电量 kWh**
  // 所以纯电车按 kWh 显示，油量策略也换成「电量够不够跑完剩余圈数」。
  const isEV = s.powertrain === 'electric';
  // 卡片标题与行标签随动力类型切换（油量→电量），术语一致
  $('lapCardTitle').textContent = isEV ? '圈速与电量' : '圈速与油量';
  $('fuelLabel').textContent = isEV ? '电量' : '油量';
  const hasEnergy = (typeof L.gas_level === 'number' && L.gas_level > 0);
  const ff = $('fFuel');
  if (!hasEnergy) {
    ff.style.width = '0'; $('oFuel').textContent = '--';
  } else if (isEV) {
    // 纯电：gas_level 是 kWh 剩余电量，没有 0~100 的百分比基准，
    // 进度条按一个假定的电池容量标尺（GT7 电车普遍 20~80 kWh）满程显示。
    const kWh = L.gas_level;
    const pctOfBattery = Math.min(100, Math.max(0, kWh / 60 * 100));
    ff.style.width = pctOfBattery + '%';
    ff.className = 'fill fuel' + (pctOfBattery < 20 ? ' low' : '');
    $('oFuel').textContent = kWh.toFixed(1) + ' kWh';
  } else {
    const fuel = Math.min(L.gas_level, 100);
    ff.style.width = fuel + '%';
    ff.className = 'fill fuel' + (fuel < 20 ? ' low' : '');
    $('oFuel').textContent = fuel.toFixed(0) + '%';
  }
  const turbo = (typeof L.turbo_boost === 'number') ? L.turbo_boost : 0;
  $('fTurbo').style.width = Math.min(turbo / 3 * 100, 100) + '%';
  $('oTurbo').textContent = turbo.toFixed(2);

  // —— 油量 / 电量策略：均耗 vs 剩余圈数 ——
  const lf = s.lap_fuel || [];
  const fs2 = $('fuelStrategy');
  const perUnit = isEV ? 'kWh' : '%';
  // 🔴 油车要求 gas_capacity > 0（有油箱才有「燃油地图」概念）；
  //    电车 gas_capacity == 0，但**更**需要这个策略（判断电量能否跑完剩余圈数），
  //    所以电车用 isEV 单独放行。
  const canStrategy = (lf.length >= 1 && L.lap != null && L.laps_in_race > 0
    && L.laps_in_race >= L.lap && (isEV || L.gas_capacity > 0));
  if (canStrategy) {
    const used = lf.reduce((a, x) => a + Math.max(0, x[1]), 0);
    const avg = used / lf.length;                       // %或 kWh / 圈
    const remain = L.laps_in_race - L.lap + 1;          // 含当前圈
    const projected = avg * remain;
    const margin = L.gas_level - projected;             // 百分点或 kWh
    // 🔴 油车可以调燃油地图省油；电车没有燃油地图，只提示电量够不够
    let advice;
    if (isEV) {
      if (margin < 0) advice = '<span style="color:var(--bad)">⚠️ 电量可能不够 → 松油门多回收 / 提高再生制动</span>';
      else advice = '<span style="color:var(--ok)">✅ 电量足够跑完</span>';
    } else if (margin < -2) {
      advice = '<span style="color:var(--bad)">⚠️ 油量不足 → 调稀燃油地图（省油优先）</span>';
    } else if (margin > 12) {
      advice = '<span style="color:var(--ok)">✅ 油量富余 → 可调浓燃油地图（动力优先）</span>';
    } else advice = '👌 油量刚好 → 维持当前燃油地图';
    fs2.innerHTML = (isEV ? '电量策略：' : '油量策略：') + '剩 ' + remain + ' 圈 · 均耗 '
      + avg.toFixed(1) + perUnit + '/圈 · 预计需 ' + projected.toFixed(0) + perUnit
      + ' · 余量 ' + (margin >= 0 ? '+' : '') + margin.toFixed(1) + perUnit + ' — ' + advice;
  } else if (lf.length >= 1) {
    fs2.textContent = '已跑 ' + lf.length + ' 圈 · 均耗 '
      + (lf.reduce((a, x) => a + Math.max(0, x[1]), 0) / lf.length).toFixed(1) + perUnit + '/圈';
  }

  // —— 每圈圈速列表（新圈在上，最快圈标绿★）——
  const lt = s.lap_times || [];
  const el = $('lapList');
  if (!lt.length) {
    el.innerHTML = '<div class="laprow empty">跑完第一圈后这里会逐圈记录</div>';
  } else {
    const best = Math.min.apply(null, lt.map(x => x[1]));
    el.innerHTML = lt.slice().reverse().map(([n, ms]) => {
      const isBest = ms === best && lt.length > 1;
      return '<div class="laprow' + (isBest ? ' best' : '') + '">' +
             '<span>第 ' + n + ' 圈</span><b>' + fmtMs(ms) + '</b></div>';
    }).join('');
  }

  drawChart(s.history);
  drawMap(s.path);
  drawGG(s.gg);

  // —— G 力球：只更新「目标位置」与读数，真正的平滑动画在 ggLoop 里 ——
  // GT7 的 g_force = [纵向, 横向]，球的 x 用横向、y 用纵向。
  const glat = (L.g_force && L.g_force[1]) || 0;
  const glon = (L.g_force && L.g_force[0]) || 0;
  ggTarget.x = glat;
  ggTarget.y = glon;
  ggLastUpdate = Date.now();
  ggReady = true;
  ggTrail.push({ x: glat, y: glon });
  if (ggTrail.length > 150) ggTrail.shift();

  $('ggLat').textContent = (glat >= 0 ? '+' : '') + glat.toFixed(2);
  $('ggLon').textContent = (glon >= 0 ? '+' : '') + glon.toFixed(2);
  const gmag = Math.hypot(glat, glon);
  $('ggTotal').textContent = gmag.toFixed(2);
  $('ggJudge').textContent =
    gmag > 2.6 ? '极限' : gmag > 1.6 ? '强过弯' :
    gmag > 0.8 ? '过弯' : gmag > 0.25 ? '巡航' : '直行';
}

function drawChart(hist) {
  if (!hist || hist.length < 2) return;
  const wrap = $('chart'); if (!wrap) return;
  // 窗口长度由个性化面板决定（约 2 / 5 / 10 秒）
  const n = Math.min(hist.length, prefs.chartWindow);
  const data = hist.slice(-n);
  const T = n / 60;                        // 窗口时长（60Hz 采样）

  // 🔴 分面（small multiples）而不是四条线挤一张图：
  //    速度 0~300、转速 0~上万、踏板 0~100%，量纲差两个数量级，
  //    共用一把尺必然有通道「看起来跳到顶」——油门的绿线爬满格
  //    纯粹是尺度假象。每个通道独立 Y 轴 + 参考网格 + 刻度值。
  // SVG 尺寸用容器实际像素宽（每 100ms 重画，跟随窗口变化），
  // 不用 viewBox 拉伸——拉伸会把刻度文字拽变形。
  const W = Math.max(320, (wrap.clientWidth || 600) | 0);
  const PADL = 6, PADR = 36;               // 右侧留白放 Y 刻度值
  const PW = W - PADL - PADR;
  const FH = 52, XLH = 15;                 // 分面绘图高 / 底部时间轴行高
  const grid = gvar('--cv-grid', 'rgba(128,128,128,.18)');
  const tcol = gvar('--cv-text', 'rgba(128,128,128,.6)');
  const x = i => PADL + PW * i / (n - 1);

  // Y 量程：速度/油门刹车按窗口数据自适应取整；转速直接用表底
  // （与转速表同一把尺，看曲线就知道离红区多远）。
  const vUnit = spdUnit();
  const vStep = vUnit === 'mph' ? 25 : 50;
  let vWin = 0;
  for (const f of data) vWin = Math.max(vWin, spd(f.speed_kph || 0));
  const vMax = Math.max(vStep, Math.ceil(vWin / vStep) * vStep);
  const rMax = rpmGaugeMax;

  const facets = [
    {k:'speed',    get:f => spd(f.speed_kph || 0),    max:vMax, ticks:[0, vMax/2, vMax]},
    {k:'rpm',      get:f => f.rpm || 0,               max:rMax, ticks:[0, rMax/2, rMax]},
    {k:'throttle', get:f => (f.throttle || 0) * 100,  max:100,  ticks:[0, 50, 100]},
    {k:'brake',    get:f => (f.brake || 0) * 100,     max:100,  ticks:[0, 50, 100]},
  ];
  const shown = facets.filter(fc => prefs.chartSeries[fc.k]);
  const meta = k => SERIES_META.find(m => m.k === k);
  const fmtTick = v => v >= 1000 ? (v / 1000) + 'k' : Math.round(v);

  let html = '';
  for (let idx = 0; idx < shown.length; idx++) {
    const fc = shown[idx], m = meta(fc.k);
    const isLast = idx === shown.length - 1;
    const H = FH + (isLast ? XLH : 0);
    const y = v => 7 + (FH - 14) * (1 - Math.min(Math.max(v / fc.max, 0), 1));
    let s = '<svg class="chsvg" width="' + W + '" height="' + H +
            '" viewBox="0 0 ' + W + ' ' + H + '">';
    // 参考网格 + Y 刻度值（0 实线，其余虚线）
    for (const tv of fc.ticks) {
      const gy = y(tv).toFixed(1);
      s += '<line x1="' + PADL + '" y1="' + gy + '" x2="' + (PADL + PW) +
           '" y2="' + gy + '" stroke="' + grid + '" stroke-width="1"' +
           (tv === 0 ? '' : ' stroke-dasharray="3 3"') + '/>';
      s += '<text class="chaxis" x="' + (PADL + PW + 4) + '" y="' + (+gy + 3) +
           '" fill="' + tcol + '">' + fmtTick(tv) + '</text>';
    }
    // 通道名（左上角，用本通道颜色，替代原来的图例）
    s += '<text x="' + (PADL + 2) + '" y="11" font-size="10" font-weight="600" fill="' +
         m.c + '">' + m.label + ' ' +
         (m.k === 'speed' ? vUnit : m.unit) + '</text>';
    // 当前时刻 = 右缘竖虚线
    s += '<line x1="' + (PADL + PW) + '" y1="7" x2="' + (PADL + PW) +
         '" y2="' + (FH - 7) + '" stroke="' + m.c + '" stroke-width="1" stroke-dasharray="2 3" opacity=".55"/>';
    // 数据线
    let d = '';
    for (let i = 0; i < n; i++) {
      d += (i === 0 ? 'M' : 'L') + x(i).toFixed(1) + ',' + y(fc.get(data[i])).toFixed(1);
    }
    s += '<path d="' + d + '" fill="none" stroke="' + m.c +
         '" stroke-width="1.6" stroke-linejoin="round"/>';
    // 底部分面画 X 轴时间窗（-5s ~ 0s），右端即「现在」
    if (isLast) {
      s += '<text class="chaxis" x="' + PADL + '" y="' + (FH + 11) +
           '" fill="' + tcol + '">-' + T.toFixed(0) + 's</text>';
      s += '<text class="chaxis" x="' + (PADL + PW / 2) + '" y="' + (FH + 11) +
           '" text-anchor="middle" fill="' + tcol + '">-' + (T / 2).toFixed(1) + 's</text>';
      s += '<text class="chaxis" x="' + (PADL + PW) + '" y="' + (FH + 11) +
           '" text-anchor="end" fill="' + m.c + '">现在 0s</text>';
    }
    s += '</svg>';
    html += s;
  }
  wrap.innerHTML = html;
  $('range').textContent = '最近 ' + n + ' 帧 / 约 ' + T.toFixed(1) +
    ' 秒 · 各通道独立刻度';
}

// ---------- 圈速格式化 ----------
function fmtMs(ms) {
  if (ms == null || ms <= 0 || ms >= 4294967295) return '--:--.---';
  const t = ms / 1000;
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return m + ':' + (s < 10 ? '0' : '') + s.toFixed(3);
}

// ---------- G 力 → 颜色（蓝→绿→橙→红）----------
function gColor(g) {
  const t = Math.min(Math.max(g, 0) / 3, 1);
  const lerp = (a, b, k) => a + (b - a) * k;
  let r, gr, b;
  if (t < 0.34) {
    const k = t / 0.34;
    r = lerp(13, 25, k); gr = lerp(110, 135, k); b = lerp(253, 84, k);
  } else if (t < 0.67) {
    const k = (t - 0.34) / 0.33;
    r = lerp(25, 253, k); gr = lerp(135, 126, k); b = lerp(84, 20, k);
  } else {
    const k = (t - 0.67) / 0.33;
    r = lerp(253, 220, k); gr = lerp(126, 53, k); b = lerp(20, 69, k);
  }
  return 'rgb(' + (r | 0) + ',' + (gr | 0) + ',' + (b | 0) + ')';
}

// ---------- 行车轨迹（按 G 力着色）----------
function drawMap(path) {
  const cv = $('map'); if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height, PAD = 20;
  ctx.clearRect(0, 0, W, H);

  if (!path || path.length < 2) {
    ctx.fillStyle = gvar('--cv-text', 'rgba(128,128,128,.65)');
    ctx.font = '13px system-ui,-apple-system,sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('等待赛道数据…（车开动后自动绘制）', W / 2, H / 2);
    $('mapinfo').textContent = '';
    return;
  }

  // 包围盒
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
  for (const p of path) {
    if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0];
    if (p[1] < z0) z0 = p[1]; if (p[1] > z1) z1 = p[1];
  }
  const spanX = Math.max(x1 - x0, 1), spanZ = Math.max(z1 - z0, 1);
  const sc = Math.min((W - 2 * PAD) / spanX, (H - 2 * PAD) / spanZ);
  const ox = (W - spanX * sc) / 2 - x0 * sc;
  const oy = (H - spanZ * sc) / 2 - z0 * sc;
  // ⚠️ 画面 y 轴向下，而 GT7 的 z 也是向下为正；
  //    这里对 z 取反，画出来的方向才和游戏里的小地图一致。
  const px = x => ox + x * sc;
  const py = z => H - (oy + z * sc);

  // 逐段着色：每段取两端较大的 G 值
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = 2.4;
  for (let i = 1; i < path.length; i++) {
    const g = Math.max(path[i][2] || 0, path[i - 1][2] || 0);
    ctx.strokeStyle = gColor(g);
    ctx.beginPath();
    ctx.moveTo(px(path[i - 1][0]), py(path[i - 1][1]));
    ctx.lineTo(px(path[i][0]), py(path[i][1]));
    ctx.stroke();
  }

  // 起点（绿圈）与当前位置（白点）
  const s0 = path[0], sN = path[path.length - 1];
  ctx.fillStyle = gvar('--ok', '#198754');
  ctx.beginPath(); ctx.arc(px(s0[0]), py(s0[1]), 5, 0, Math.PI * 2); ctx.fill();
  // 当前位置：卡片底色填充 + 强调色描边，深浅主题上都看得见
  ctx.fillStyle = gvar('--card', '#fff');
  ctx.strokeStyle = gvar('--accent', '#0d6efd'); ctx.lineWidth = 2.5;
  ctx.beginPath(); ctx.arc(px(sN[0]), py(sN[1]), 5.5, 0, Math.PI * 2);
  ctx.fill(); ctx.stroke();

  $('mapinfo').textContent = path.length + ' 点 · 最高 ' +
    Math.max.apply(null, path.map(p => p[2] || 0)).toFixed(1) + 'g';
}

// ---------- G-G 散点图（抓地力圆）----------
// 保留原样：把整场比赛的 (横向G, 纵向G) 打成散点，
// 用来看这辆车/这条赛道把轮胎用到什么程度（抓地力圆）。
function drawGG(gg) {
  const cv = $('gg'); if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  const cx = W / 2, cy = H / 2;
  const GMAX = GG_MAX;                    // 量程跟着个性化面板走
  const R = Math.min(W, H) / 2 - 16;
  const sc = R / GMAX;
  const CV_TEXT = gvar('--cv-text', 'rgba(128,128,128,.65)');
  ctx.clearRect(0, 0, W, H);

  ctx.lineWidth = 1;
  for (let g = 1; g <= GMAX; g++) {
    // 外圈 = 量程边界，用最重的 --cv-edge；内圈参考环用 --cv-ring
    ctx.strokeStyle = g === GMAX ? gvar('--cv-edge') : gvar('--cv-ring');
    ctx.beginPath(); ctx.arc(cx, cy, sc * g, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.strokeStyle = gvar('--cv-axis', 'rgba(128,128,128,.35)');
  ctx.beginPath();
  ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
  ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
  ctx.stroke();

  if (gg && gg.length) {
    const n = gg.length;
    for (let i = 0; i < n; i++) {
      const lat = gg[i][0], lon = gg[i][1];
      const x = cx + lat * sc;
      const y = cy - lon * sc;
      ctx.globalAlpha = 0.12 + 0.55 * (i / n);
      ctx.fillStyle = gColor(Math.hypot(lat, lon));
      ctx.beginPath(); ctx.arc(x, y, 1.7, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  ctx.fillStyle = CV_TEXT;
  ctx.font = '10px system-ui,-apple-system,sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('加速', cx, cy - R - 5);
  ctx.fillText('刹车', cx, cy + R + 12);
  ctx.textAlign = 'left';
  ctx.fillText('右转', cx + R + 3, cy + 3);
  ctx.textAlign = 'right';
  ctx.fillText('左转', cx - R - 3, cy + 3);
  if (!gg || !gg.length) {
    ctx.fillStyle = CV_TEXT;
    ctx.textAlign = 'center';
    ctx.font = '12px system-ui,-apple-system,sans-serif';
    ctx.fillText('过弯后自动生成', cx, cy - 6);
  }
}

// ---------- G 力球（实时摇晃，仿 SU7 那种）----------
// 与上面的散点图互补：
//   · 散点图 = 整场的「抓地力圆」（慢变量，看极限用到多少）
//   · G 力球 = 当前这一刻的 G（快变量，跟着方向盘和踏板实时晃）
// 🔴 let 而不是 const：个性化面板的「G 力量程」会改它，抓地力图与 G 力球共用。
let GG_MAX = 3;
let ggBall = { x: 0, y: 0 };      // 平滑后的球位（单位 g）
let ggTarget = { x: 0, y: 0 };    // 目标球位（来自最新帧）
let ggTrail = [];                 // 最近轨迹（g 坐标）
let ggReady = false;
let ggLastUpdate = 0;             // 上次收到数据的时间（用于停数据后回正）

// 之前这里判断 prefers-color-scheme —— 换成偏好主题后会在深色系统上失效
// （用户选了浅色，球还是按深色画）。现在直接读当前主题的变量值。
function ggTheme() {
  return {
    bowl: gvar('--cv-bowl', 'rgba(0,0,0,.03)'),
    ring: gvar('--cv-ring', 'rgba(0,0,0,.08)'),
    edge: gvar('--cv-edge', 'rgba(0,0,0,.18)'),
    axis: gvar('--cv-axis', 'rgba(0,0,0,.12)'),
    text: gvar('--cv-text', 'rgba(0,0,0,.5)'),
    shadow: gvar('--cv-shadow', 'rgba(0,0,0,.22)'),
  };
}

function drawBall() {
  const cv = $('gball'); if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  const cx = W / 2, cy = H / 2;
  const R = Math.min(W, H) / 2 - 22;
  const sc = R / GG_MAX;
  const th = ggTheme();
  ctx.clearRect(0, 0, W, H);

  // 浅碗底盘
  ctx.fillStyle = th.bowl;
  ctx.beginPath(); ctx.arc(cx, cy, R + 9, 0, Math.PI * 2); ctx.fill();

  // 参考环
  ctx.lineWidth = 1;
  for (let g = 1; g <= GG_MAX; g++) {
    ctx.strokeStyle = (g === GG_MAX) ? th.edge : th.ring;
    ctx.beginPath(); ctx.arc(cx, cy, sc * g, 0, Math.PI * 2); ctx.stroke();
  }
  // 十字轴
  ctx.strokeStyle = th.axis;
  ctx.beginPath();
  ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
  ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
  ctx.stroke();

  // 近端轨迹（越新越亮）
  const n = ggTrail.length;
  for (let i = 0; i < n; i++) {
    const t = ggTrail[i];
    ctx.globalAlpha = 0.04 + 0.26 * (i / Math.max(1, n));
    ctx.fillStyle = gColor(Math.hypot(t.x, t.y));
    ctx.beginPath();
    ctx.arc(cx + t.x * sc, cy - t.y * sc, 2.4, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  // 小球（带高光，做出立体感）
  const bx = cx + ggBall.x * sc;
  const by = cy - ggBall.y * sc;
  const gmag = Math.hypot(ggBall.x, ggBall.y);
  ctx.shadowColor = th.shadow; ctx.shadowBlur = 12; ctx.shadowOffsetY = 3;
  const grad = ctx.createRadialGradient(bx - 5, by - 6, 1.5, bx, by, 17);
  grad.addColorStop(0, 'rgba(255,255,255,.95)');
  grad.addColorStop(0.45, gColor(gmag));
  grad.addColorStop(1, gColor(gmag));
  ctx.fillStyle = grad;
  ctx.beginPath(); ctx.arc(bx, by, 16, 0, Math.PI * 2); ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

  // 中心→球的连线，强化方向感
  if (gmag > 0.08) {
    ctx.strokeStyle = 'rgba(140,140,140,.55)';
    ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(bx, by); ctx.stroke();
  }

  // 方位标签
  ctx.fillStyle = th.text;
  ctx.font = '10px system-ui,-apple-system,sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('加速', cx, cy - R - 10);
  ctx.fillText('刹车', cx, cy + R + 18);
  ctx.textAlign = 'left';
  ctx.fillText('右转', cx + R + 5, cy + 3);
  ctx.textAlign = 'right';
  ctx.fillText('左转', cx - R - 5, cy + 3);

  if (!ggReady) {
    ctx.fillStyle = th.text;
    ctx.textAlign = 'center';
    ctx.font = '12px system-ui,-apple-system,sans-serif';
    ctx.fillText('等待车辆数据…', cx, cy - 4);
  }
}

// 平滑动画：让球「摇」起来，而不是硬跳
function ggLoop() {
  // 超过 2 秒没收到数据 → 让球慢慢回到中心，避免停表时球卡在角落
  if (ggLastUpdate && Date.now() - ggLastUpdate > 2000) {
    ggTarget.x = 0;
    ggTarget.y = 0;
  }
  const k = 0.16;                 // 追赶系数，越小越黏
  ggBall.x += (ggTarget.x - ggBall.x) * k;
  ggBall.y += (ggTarget.y - ggBall.y) * k;
  drawBall();
  requestAnimationFrame(ggLoop);
}
// ---------- 术语说明面板 ----------
// 🔴 这个函数之前**压根没定义**，但关闭按钮和遮罩都在调用它 ——
//    点「术语说明」只会跳到页面顶部（href="#"），面板永远打不开，
//    就算强行打开，关闭按钮也会抛 ReferenceError。
function toggleApiPanel(open) {
  $('apiWrap').classList.toggle('open', !!open);
}
function toggleGlossary(open) {
  const w = $('glossWrap');
  if (w) w.classList.toggle('open', !!open);
}
// ESC 关闭
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    toggleGlossary(false); toggleApiPanel(false); togglePrefPanel(false);
  }
});

// 拉多少帧跟着面板的曲线窗口走：窗口拉到 10 秒，这里就得要 600 帧，
// 否则曲线会因为数据不够而画不满。
function poll() {
  fetch('/api/state?frames=' + prefs.chartWindow).then(r => r.json()).then(render)
    .catch(() => { $('dot').className = 'dot off';
      $('status').textContent = '服务无响应'; });
}
poll();
setInterval(poll, 100);   // 10Hz 轮询；G 力球靠 rAF 在两次轮询之间平滑插值
ggLoop();                 // 启动 G 力球动画（自带 requestAnimationFrame 循环）

// ---------- 布局系统：卡片显隐 / 半宽整行 / 拖拽排序 / 多布局保存 ----------
// 存 localStorage（每个浏览器各一份）——布局是视觉偏好，跟屏幕尺寸相关，
// 本来就应该按设备存；也避免给服务端加写入接口。
const LAYOUT_KEY = 'gt7_layouts_v2';   // v2：默认布局重排过，废弃旧存档
const CARD_TITLES = {
  'c-rpm':'转速与速度', 'c-pedal':'踏板与 G 力', 'c-lap':'圈速与能量',
  'c-gball':'G 力球', 'c-gg':'G-G 图', 'c-map':'行车轨迹',
  'c-chart':'实时曲线', 'c-wheel':'四轮状态',
  'c-engine':'引擎健康', 'c-race':'比赛信息',
};
const DEFAULT_CARDS = [
  {id:'c-rpm',  span:1, show:true},
  {id:'c-pedal',span:1, show:true},
  {id:'c-lap',  span:1, show:true},
  {id:'c-gball',span:1, show:true},
  {id:'c-gg',   span:1, show:true},
  {id:'c-wheel',span:1, show:true},
  {id:'c-engine',span:1, show:true},
  {id:'c-race', span:1, show:true},
  {id:'c-map',  span:2, show:true},
  {id:'c-chart',span:2, show:true},
];
let layoutState = {
  layouts: [{name:'默认布局', builtin:true,
             cards: JSON.parse(JSON.stringify(DEFAULT_CARDS))}],
  current: '默认布局',
};
(function () {
  try {
    const s = JSON.parse(localStorage.getItem(LAYOUT_KEY));
    // 校验：布局数组必须存在且每张卡都有 id，否则弃用存档
    if (s && Array.isArray(s.layouts) && s.layouts.length &&
        s.layouts.every(l => Array.isArray(l.cards) &&
          l.cards.every(c => document.getElementById(c.id)))) {
      layoutState = s;
    }
  } catch (e) { /* 存档坏了就用默认 */ }
  // 版本升级对齐：旧存档缺少后来新增的卡片时，按默认顺序补进去。
  // 不做这一步，升级后新卡会「存在但排在错位/不可见」，用户会当成 bug。
  // 已有卡片的位置与显隐保持不变，只做「缺啥补啥」。
  layoutState.layouts.forEach(function (l) {
    DEFAULT_CARDS.forEach(function (d) {
      if (!l.cards.some(function (c) { return c.id === d.id; })) {
        l.cards.push(JSON.parse(JSON.stringify(d)));
      }
    });
  });
})();

function curLayout() {
  return layoutState.layouts.find(l => l.name === layoutState.current) ||
         layoutState.layouts[0];
}
function persist() {
  try { localStorage.setItem(LAYOUT_KEY, JSON.stringify(layoutState)); }
  catch (e) { /* 隐私模式等场景写不进就算了 */ }
}

// 按 layoutState 重排 DOM：appendChild 会把节点移到末尾，顺序即 cards 顺序
function applyLayout() {
  const grid = $('cardGrid'); if (!grid) return;
  curLayout().cards.forEach(c => {
    const el = document.getElementById(c.id); if (!el) return;
    grid.appendChild(el);
    el.classList.toggle('span2', c.span === 2);
    el.style.display = c.show ? '' : 'none';
  });
  persist();
}
function setCardShow(id, show) {
  const c = curLayout().cards.find(x => x.id === id); if (!c) return;
  c.show = show; applyLayout(); renderCardToggles();
}
function setCardSpan(id, span) {
  const c = curLayout().cards.find(x => x.id === id); if (!c) return;
  c.span = +span; applyLayout(); renderCardToggles();
}
function switchLayout(name) {
  layoutState.current = name; persist(); applyLayout(); renderLayoutPanel();
}
function saveLayoutAs() {
  const name = ($('layoutName').value || '').trim();
  if (!name) { $('layoutName').placeholder = '请先输入布局名称'; return; }
  const cards = JSON.parse(JSON.stringify(curLayout().cards));
  const ex = layoutState.layouts.find(l => l.name === name);
  if (ex) ex.cards = cards;
  else layoutState.layouts.push({ name: name, cards: cards });
  layoutState.current = name; persist(); applyLayout(); renderLayoutPanel();
}
function deleteLayout() {
  const lay = curLayout();
  if (lay.builtin) return;
  if (!confirm('删除布局「' + lay.name + '」？')) return;
  layoutState.layouts = layoutState.layouts.filter(l => l !== lay);
  layoutState.current = layoutState.layouts[0].name;
  persist(); applyLayout(); renderLayoutPanel();
}
function resetLayout() {
  curLayout().cards = JSON.parse(JSON.stringify(DEFAULT_CARDS));
  persist(); applyLayout(); renderLayoutPanel();
}
function renderLayoutPanel() {
  $('layoutSel').innerHTML = layoutState.layouts.map(l =>
    '<option' + (l.name === layoutState.current ? ' selected' : '') + '>' +
    l.name + '</option>').join('');
  $('btnDelLayout').disabled = !!curLayout().builtin;
  renderCardToggles();
}
function renderCardToggles() {
  $('cardToggles').innerHTML = curLayout().cards.map(c =>
    '<div class="trow"><label><input type="checkbox"' + (c.show ? ' checked' : '') +
    ' onchange="setCardShow(\'' + c.id + '\',this.checked)"> ' + CARD_TITLES[c.id] +
    '</label><select onchange="setCardSpan(\'' + c.id + '\',this.value)">' +
    '<option value="1"' + (c.span === 1 ? ' selected' : '') + '>半宽</option>' +
    '<option value="2"' + (c.span === 2 ? ' selected' : '') + '>整行</option>' +
    '</select></div>').join('');
}
function toggleLayoutPanel(open) {
  $('layoutWrap').classList.toggle('open', !!open);
  document.body.classList.toggle('layout-editing', !!open);
  // 面板打开 = 进入编辑模式：卡片显示虚线框、可拖动
  document.querySelectorAll('#cardGrid .card').forEach(el => {
    el.classList.toggle('editmode', !!open);
    el.draggable = !!open;
  });
  if (open) { applyLayout(); renderLayoutPanel(); }
  else syncFromDOM();
}
// 面板关闭时按 DOM 顺序回写布局（拖拽过程中只动 DOM，结束时才同步）
function syncFromDOM() {
  const order = [];
  $('cardGrid').querySelectorAll('.card').forEach(el => { if (el.id) order.push(el.id); });
  curLayout().cards.sort((a, b) =>
    order.indexOf(a.id) - order.indexOf(b.id));
  persist();
}
// 拖拽（事件委托到容器；面板没开时不响应）
(function () {
  const grid = $('cardGrid'); if (!grid) return;
  let dragId = null;
  grid.addEventListener('dragstart', function (e) {
    const card = e.target.closest('.card');
    if (!card || !$('layoutWrap').classList.contains('open')) { e.preventDefault(); return; }
    dragId = card.id;
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', card.id); } catch (err) {}
  });
  grid.addEventListener('dragover', function (e) {
    if (!dragId) return;
    e.preventDefault();
    const over = e.target.closest('.card');
    if (!over || over.id === dragId) return;
    const dragging = document.getElementById(dragId);
    const r = over.getBoundingClientRect();
    // 半宽卡按鼠标在卡的左右哪半边决定插前/插后；整行卡按上下
    const before = over.classList.contains('span2')
      ? e.clientY < r.top + r.height / 2
      : e.clientX < r.left + r.width / 2;
    grid.insertBefore(dragging, before ? over : over.nextSibling);
  });
  grid.addEventListener('drop', function (e) { e.preventDefault(); });
  grid.addEventListener('dragend', function () {
    if (dragId) syncFromDOM();
    dragId = null;
  });
})();
applyLayout();            // 启动时按保存的布局排列一次
// 放在最后：applyPrefs() 会读写 GG_MAX / 依赖已声明完的函数，
// 提前调用会撞上 let 的暂时性死区。
applyPrefs();             // 再把主题/单位/图表偏好应用到页面上
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

def main() -> int:
    global PAGE, HUB

    # Windows 控制台默认 GBK 编码，遇到 ⚠/emoji 会 UnicodeEncodeError 直接崩溃
    # （打包成 exe 后尤其明显）。放宽为「不可编码字符用 ? 代替」即可避免。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description="GT7 遥测 Web 仪表盘")
    p.add_argument("-p", "--port", type=int, default=8787, help="监听端口，默认 8787")
    p.add_argument("--bind", default="0.0.0.0", help="绑定地址，默认 0.0.0.0")
    p.add_argument(
        "-d", "--history", default="./data",
        help="历史场次目录（用于 /api/sessions），默认 ./data",
    )
    p.add_argument(
        "-s", "--status", default="./data/status.json",
        help="接收器写的状态文件路径，默认 ./data/status.json",
    )
    args = p.parse_args()

    PAGE = build_page()

    hist = Path(args.history).resolve()
    hist.mkdir(parents=True, exist_ok=True)

    status_path = Path(args.status).resolve()
    HUB = TelemetryHub(status_path)

    class Handler(DashboardHandler):
        pass

    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    srv.daemon_threads = True
    # ⚠️ 必须挂在 server 实例上——handler 里读的是 self.server.history_dir。
    #    挂在 Handler 类上是无效的（早期版本踩过，报
    #    "'ThreadingHTTPServer' object has no attribute 'history_dir'"）。
    srv.history_dir = str(hist)  # type: ignore[attr-defined]

    print("=" * 62, flush=True)
    print("GT7 遥测 Web 仪表盘", flush=True)
    print(f"  访问地址: http://{args.bind}:{args.port}", flush=True)
    if args.bind == "0.0.0.0":
        print(f"  局域网访问: http://<本机IP>:{args.port}", flush=True)
    print(f"  状态文件: {status_path}", flush=True)
    print(f"  历史场次: {hist}", flush=True)
    if not status_path.exists():
        print("  ⚠ 状态文件不存在——请确认接收器带 --status-file 参数启动", flush=True)
    print("=" * 62, flush=True)

    def shutdown(sig: Any, frm: Any) -> None:
        print("\n收到信号，关闭…", flush=True)
        srv.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        print("已退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())