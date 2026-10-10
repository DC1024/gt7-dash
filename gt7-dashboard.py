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
import gzip
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from collections.abc import Sequence
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
        # 本圈起点（墙上时刻）。由接收器写在状态文件里，冲线时更新。
        # 0 = 老记录器没给 → 退回「场次起点」口径并如实标注。
        self._lap_started_at: float = 0.0

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
                # 缺失时置 0（不是沿用上一场！换场后沿用会算出上一圈的角度）
                self._lap_started_at = float(
                    payload.get("lap_started_at") or 0.0)
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
            # 🔴 latest 是**原始帧**，菜单态的 0xFFFF 会原样发给 /api/state 的
            #    调用方（前端自己的 okPos() 只挡 UI，挡不住第三方）。归一一次。
            if latest is not None:
                _normalize_u16_frame(latest)
            lap_time = 0.0
            if latest and self._lap_started_at:
                # 🔴 本圈已用时 = 当前帧时刻 − **本圈起点**。
                #    早先这里减的是 session_start，于是主界面那个圈速大计时器
                #    显示的其实是「场次已用时」：跑第 3 圈时它显示的是三圈的
                #    累计时间，冲线也不归零。v1 的 current_lap_time_s 文档写的是
                #    「本圈已用时」，也对不上。
                lap_time = max(0.0, latest["t"] - self._lap_started_at)
            elif latest and self._session_start:
                # 兜底：老记录器不写 lap_started_at。绝不静默给错数——
                # 用 lap_time_source 标出来，消费方自己决定信不信。
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
                # 🔴 当前圈起点的墙上时刻：接收器每次冲线更新。
                #    仪表盘用它反推上一圈用时，作为 GT7 不报 last_lap 时的兜底圈速来源。
                "lap_started_at": self._lap_started_at,
                "lap_fuel": self._lap_fuel,
                "powertrain": self._powertrain,
                "max_energy_recovery": self._max_energy_recovery,
                "car_name": self._car_name_of(latest),
                "session_duration": round(time.time() - self._session_start, 1)
                if self._session_start
                else 0,
                "lap_time": round(lap_time, 3),
                "lap_time_source": ("lap" if self._lap_started_at
                                    else "session"),
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

# 场次 jsonl 解析缓存：path -> (mtime, size, header, store)。
# 🔴 /session 一次请求会跑 analyze_session + compare_session 两个分析，
#    各自把 250MB jsonl 读一遍纯属浪费；切换参考圈(?ref_lap=N)更是
#    反复重读同一文件。缓存解析结果后，二次请求只算不读，
#    页面响应从秒级降到亚秒级。jsonl 落盘后不可变，mtime+size 失效足够。
_FRAMES_CACHE: dict[str, tuple[float, int, dict, "FrameStore"]] = {}
# ThreadingHTTPServer 是多线程的：缓存的读-改-写必须加锁，否则并发请求下
# dict 可能在迭代中被另一个线程修改（RuntimeError / 读到半成品条目）。
_CACHE_LOCK = threading.RLock()

# —— 逐帧字段里「全库真正会读」的那些 ——
# 实测一场 217475 帧的场次有 52 个逐帧字段，但代码真正会读的只有下面 16 个。
# 剩下 36 个（tyre_press / seq / position / hand_brake / susp_height …）
# 全库没有一处读，存进内存纯属白占 —— 而内存正是原来的瓶颈。
#
# 🔴 以后要读新字段，必须加进 _FRAME_COL_KIND，否则读到的是 None。
#    忘了加也不会静默出错：FrameStore 会把「读了但没存的字段」记进 MISSED_FIELDS，
#    tests/test_frame_store.py 会因此失败。
# 每个存储字段的落地方式：'n'=float64 标量列 / 'a'=float64 数组列（如 g_force）
# / 'b'=布尔 / 's'=字符串（内部表 + 索引）
_FRAME_COL_KIND: dict[str, str] = {
    "t": "n", "lap": "n", "speed_kph": "n", "rpm": "n",
    "throttle": "n", "brake": "n", "gear": "n",
    "car_x": "n", "car_z": "n",
    "gas_level": "n", "gas_capacity": "n", "car_code": "n",
    # 🔴 比赛进行中 quali_pos(0x84) = **当前名次**（逐帧实时变）——进站与名次
    #    时间线卡（/pitstops）的唯一名次来源。此前全库无人读所以没存；
    #    加入后老场次照样能读出（recorder 落盘就有该字段）。
    "quali_pos": "n",
    "g_force": "a",
    # 四轮角速度（rad/s，顺序 前左/前右/后左/后右）。
    # 只有它是「车轮实际转多快」的唯一来源 —— 车身速度传感器测不出空转和抱死，
    # 轮胎滑移检测（gt7analysis.wheel_slip）全靠它。一场 217k 帧多占约 7 MB。
    "wheel_rads": "a",
    # 四轮胎温（℃）与悬挂行程（m）：事件卡的「轮胎滥用」证据要读。
    # 🔴 实测（两场真实 jsonl）确认这两个字段**有真值**（胎温 54~97℃、
    #    悬挂 0.0~0.3m）；而 tyre_press / tyre_wear 在格式 A 下**恒为 0**
    #    （GT7 不广播），所以那两个**不许加** —— 加了只会让事件证据里
    #    出现一堆 0，看着像有数据其实是假的。
    "tyre_temp": "a",
    "susp_height": "a",
    "has_coords": "b",
    "layout": "s",
}
_FRAME_FIELDS = frozenset(_FRAME_COL_KIND)

# 「代码读了、但没存」的字段名（进程级）。空 = 存储清单是完整的。
MISSED_FIELDS: set[str] = set()
_warned_fields: set[str] = set()

# 内部哨兵：区分「键不存在」与「键存在但值是 null」——这是 dict 语义的一部分，
# 必须分清，否则 f.get("x") 会给错默认值。
_ABSENT = object()
# 「整场所有帧都命中」的标记，用来避免为「整场都是 null」（如纯电车的油量口径）
# 或「整场都缺席」的字段存 21 万个下标。
_ALL = object()


# 字段取值的列种类。预先摊平成一维（见 FrameStore._build_index）——
# 旧写法每取一个字段要依次试 _absent/_null/_num/_arr/_bool/_txi 六张表，
# 217k 帧的 clean_laps 一趟就多花几十毫秒（实测单次取值 273ns → 摊平后 1xx ns）。
_K_NUM, _K_AXIS, _K_BOOL, _K_STR, _K_MISS = 1, 2, 3, 4, 0


class Frame:
    """一帧的**只读视图**：不复制数据，按需从列里取值。

    为什么不继续用 dict：一场 217k 帧 × 52 键的 dict 实测要 1550 MB
    （7467 字节/帧）。视图只是个 (store, index) 二元组，按需读列，
    不产生 per-frame 的 dict —— 这是内存能降下来的关键。

    语义上与原来的 dict 完全一致：`f["k"]` 键不存在抛 KeyError，
    `f.get("k")` 键存在但值为 null 时返回 None、键不存在时返回默认值。
    """

    __slots__ = ("_s", "_i")

    def __init__(self, store: "FrameStore", i: int) -> None:
        self._s = store
        self._i = i

    def __getitem__(self, k: str):
        d = self._s._col.get(k)
        # 🔴 快路：干净的数字列（无缺席、无 null）就地取值，不下沉到 _value。
        #    详情页一次渲染要取 500 万次字段值，省下的这一层函数调用
        #    在慢一点的机器上是实打实的几百毫秒。
        if d is not None and d[4]:
            return d[1][self._i]
        v = self._s._value_d(k, self._i, d)
        if v is _ABSENT:
            raise KeyError(k)
        return v

    def get(self, k: str, default=None):
        d = self._s._col.get(k)
        if d is not None and d[4]:
            return d[1][self._i]
        v = self._s._value_d(k, self._i, d)
        return default if v is _ABSENT else v

    def __contains__(self, k: str) -> bool:
        """键在不在这帧里。

        🔴 不复用 _value：判键的调用频次和取值一样高（`[f for f in frames
           if "lap" in f]` 是每个接口的必经之路），但只需要「在不在」这一个
           比特。存储时已按字段算好一张存在位图，这里直接查表，217k 帧一趟
           比真的取 217k 次值快一倍。
        """
        p = self._s._pres.get(k)
        if p is True:
            return True
        if p is None or p is False:
            return False
        return p[self._i] != 0

    def __repr__(self) -> str:
        return f"<Frame #{self._i}>"


class FrameStore(Sequence):
    """场次帧的列式存储（float64），对外表现得像一个 list[Frame]。

    精度（实测，见 _ui_check/probe_precision2.py）：
      · 数值一律 float64（`array('d')`）。逐个数值比对 `float.hex()`，
        与「json.loads 出来的原始 float」**位级完全相同** —— 零精度损失、
        零显示损失，/series、/frames、CSV 的数值文本一个字符都不变。
      · 🔴 不能用 float32：t 是 Unix 时间戳(≈1.79e9)，float32 在那个量级的
        最小间隔是 128 秒（帧间隔才 0.0167 秒）；而且 json.dumps(float32(354.0125))
        会吐出 "354.01251220703125"，直接把 API/CSV 的数值文本搞脏。
      · 派生指标也验证过：圈长差 ±2e-6 m、滑移率差 1e-7，可忽略。
    """

    __slots__ = ("header", "n", "fields", "_num", "_arr", "_bool", "_tbl",
                 "_txi", "_null", "_absent", "_col", "_pres", "_memo",
                 "_memo_lock", "_memo_keys")

    def __init__(self, header: dict, n: int, num: dict, arr: dict, bl: dict,
                 tbl: dict, txi: dict, null: dict, absent: dict,
                 fields: set[str]) -> None:
        self.header = header
        self.n = n
        self.fields = fields
        self._num = num
        self._arr = arr
        self._bool = bl
        self._tbl = tbl
        self._txi = txi
        self._null = null
        self._absent = absent
        self._memo: dict = {}
        # 🔴 _memo 是**跨请求共享**的（缓存的 store 会被多个线程同时用），
        #    读-改-写必须加锁，见 memo_compute。
        self._memo_lock = threading.Lock()
        self._memo_keys: dict = {}
        self._col, self._pres = self._build_index()

    def memo_compute(self, key, fn):
        """按 key 记忆化 `fn()` 的结果（线程安全版）。

        ThreadingHTTPServer 是多线程的：/sectors 冷 0.4s、/events 冷 9s，
        两个请求同时打进来会各算一遍；更糟的是 dict 的读-改-写本身不原子，
        并发下可能读到半成品条目。

        🔴 用**按 key 一把锁**而不是全局一把：
           · 同一个 key 并发 ⇒ 第二个线程等第一个算完直接拿结果，
             不会把 9 秒的事件检测烧两遍（1 核小机器上这是能不能扛住的区别）。
           · 不同 key 互不阻塞 ⇒ 正在算 /events 时不该把读 valid_laps
             的 /series 一起堵死 9 秒。
        """
        with self._memo_lock:
            hit = self._memo.get(key)
            if hit is not None:
                return hit
            kl = self._memo_keys.get(key)
            if kl is None:
                kl = self._memo_keys[key] = threading.Lock()
        with kl:
            with self._memo_lock:
                hit = self._memo.get(key)
            if hit is not None:
                return hit
            val = fn()
            with self._memo_lock:
                cur = self._memo.get(key)
                if cur is None:
                    self._memo[key] = val
                    return val
                return cur        # 理论上到不了：同一 key 已串行

    @classmethod
    def empty(cls, header: dict | None = None) -> "FrameStore":
        return cls(header or {}, 0, {}, {}, {}, {}, {}, {}, {}, set())

    def _build_index(self) -> tuple[dict, dict]:
        """把「字段名 → 取值路径」预摊平成一张表；另算一张「键在不在」的位图。

        _col[k] = (种类, 数据, 缺席标记, null 标记, 是否快路)
        这样取一个字段只查一次 dict，而不再是依次试六张表。
        第 5 位（快路）预先算好，让最热的「干净数字列」在 Frame 里
        一个下标 + 一次真值判断就能取值，不必再进 _value 绕一圈。
        _pres[k] = True（恒定在）/ False（恒定不在）/ bytearray（逐帧看位）。
        """
        col: dict[str, tuple] = {}
        for k in self.fields:
            a = self._absent.get(k)
            nl = self._null.get(k)
            data = None
            if k in self._num:
                kind, data = _K_NUM, self._num[k]
            elif k in self._arr:
                kind, data = _K_AXIS, tuple(self._arr[k])
            elif k in self._bool:
                kind, data = _K_BOOL, self._bool[k]
            elif k in self._txi:
                kind, data = _K_STR, (self._tbl[k], self._txi[k])
            else:
                # 本场次出现过、但没进存储清单：读出来是 None 并计入 MISSED_FIELDS
                kind = _K_MISS
            fast = kind == _K_NUM and a is None and nl is None
            col[k] = (kind, data, a, nl, fast)

        pres: dict[str, object] = {}
        for k, d in col.items():
            a = d[2]
            if a is None:
                pres[k] = True
            elif a is _ALL:
                pres[k] = False
            else:
                m = bytearray(b"\x01" * self.n)     # 1 = 这帧有这个键
                for j in a:
                    m[j] = 0
                pres[k] = m
        return col, pres

    def __len__(self) -> int:
        return self.n

    def __getitem__(self, i):
        if isinstance(i, slice):
            return self.frames()[i]
        i = int(i)
        if i < 0:
            i += self.n
        if not 0 <= i < self.n:
            raise IndexError(i)
        return Frame(self, i)

    def __iter__(self):
        return iter(self.frames())

    def __bool__(self) -> bool:
        return self.n > 0

    def frames(self) -> list["Frame"]:
        """全部帧的 Frame 列表（按原顺序）。整场只建一次，然后留在记忆里。

        🔴 为什么必须记忆化：`Frame` 是「每次遍历现造」的视图对象，而
           analyze_session 渲染一趟要遍历 store 六遍（算最高速、算主车型、
           取圈分组…）。旧实现遍历的是 list[dict]，迭代本身零成本；若每遍
           都新建 217k 个 Frame，就要把这份开销付六遍 —— 服务端实测
           analyze_session 因此比旧版慢 23%（0.498s → 0.652s，其中
           __iter__ 重入 130 万次 0.63s + Frame.__init__ 0.15s），
           整页 /session 慢 8%。收敛成一次后，后续遍历退化为纯 list 迭代。
        """
        return self.memo_compute(
            "frames", lambda: [Frame(self, i) for i in range(self.n)])

    def lap_frames(self) -> list["Frame"]:
        """含 lap 字段的帧（保持原顺序）。

        🔴 每个详情页接口的第一件事都是这个列表，而它只取决于场次内容、
           场次不变它就不变。以前每次请求都对 217k 帧重建一遍（还要新建
           217k 个 Frame 对象）；现在整场只算一次，后续请求 O(1) 取用。
           clean_laps 也喂它，两者共享同一批 Frame 对象，不额外占内存。
        """
        return self.memo_compute(
            "lap_frames", lambda: [f for f in self.frames() if "lap" in f])

    def _missing(self, k: str):
        """本场次该字段确实存在，但列式存储里没留它的列 —— 是存储清单漏了。

        空转返回 None（与旧 dict 实现「键在、值为 null」表现一致），
        同时记一笔，由测试守住（tests/test_frame_store.py）。
        """
        MISSED_FIELDS.add(k)
        if k not in _warned_fields:
            _warned_fields.add(k)
            print(f"[frames] ⚠️ 读了未存储的字段 {k!r}：请把它加进 "
                  f"_FRAME_COL_KIND，否则本处静默拿到 None",
                  file=sys.stderr, flush=True)
        return None

    def _value(self, k: str, i: int):
        """取一帧某字段的值（语义参考实现）。

        🔴 「键缺席」与「值是 null」必须分开：前者 `.get()` 返回默认值、
           `[]` 抛 KeyError；后者 `.get()` 返回 None、`[]` 返回 None。
           混为一谈会让 `f.get("x") or 0` 之类的写法结果不同。

        Frame 的几个取值方法会自己走「干净数字列」的快路，走到这里的是
        慢路；两边口径由 tests/test_frame_store.py 的 TestPresence /
        TestFidelity 逐帧锁死。
        """
        return self._value_d(k, i, self._col.get(k))

    def _value_d(self, k: str, i: int, d):
        """同上，但调用方已经查过 _col —— 省掉一次 dict 查找。"""
        if d is None:
            return _ABSENT
        kind = d[0]
        if kind == _K_MISS:
            return self._missing(k)
        a = d[2]
        if a is not None and (a is _ALL or i in a):
            return _ABSENT
        nl = d[3]
        if nl is not None and (nl is _ALL or i in nl):
            return None
        data = d[1]
        if kind == _K_NUM:
            return data[i]
        if kind == _K_AXIS:
            return [c[i] for c in data]
        if kind == _K_BOOL:
            return data[i] != 0
        tb, xi = data
        return tb[xi[i]]


# —— 归档：老场次就地压缩成 .jsonl.gz ——
#
# 60Hz × 52 个字段 ≈ 240 MB/小时，一两天就能把盘吃光。归档的办法不是降采样
# （丢帧不可逆，遥测的原始价值就没了），而是**就地 gzip**：jsonl 是重复度极高
# 的纯文本，实测 255 MB → 30 MB 左右，且解压后逐字节相同。
#
# 🔴 名字保持 `X.jsonl.gz` 而不是改名：收藏、自定义名、赛道库、书签里存的
#    都是 `X.jsonl`，所以访问一个已被归档的场次时要用 resolve_session 兜住。
SESSION_SUFFIXES = (".jsonl", ".jsonl.gz")


def is_session_file(p: Path) -> bool:
    n = str(getattr(p, "name", p))
    return n.endswith(".jsonl") or n.endswith(".jsonl.gz")


def session_stem(name: str) -> str:
    """去掉场次后缀（.jsonl 或 .jsonl.gz）—— 导出 CSV 时当基名用。"""
    n = str(name)
    for s in (".jsonl.gz", ".jsonl"):
        if n.endswith(s):
            return n[:-len(s)]
    return Path(n).stem


def open_session(path: Path, mode: str = "r"):
    """按后缀决定要不要解压：.gz 走 gzip.open，其余就是普通文本。"""
    p = Path(path)
    if p.name.endswith(".gz"):
        return gzip.open(p, mode.replace("b", "") + "t",
                         encoding="utf-8", errors="replace")
    return p.open(mode, encoding="utf-8", errors="replace")


def resolve_session(target: Path) -> Path:
    """把「请求的场次路径」落到真实存在的文件上。

    归档后磁盘上只有 `X.jsonl.gz`，但书签 / 收藏 / 赛道库里存的还是 `X.jsonl`，
    直接判 exists() 会 404。这里补一次 .gz 尝试。
    """
    if target.exists():
        return target
    gz = Path(str(target) + ".gz")
    return gz if gz.exists() else target


def glob_sessions(history_dir: Path) -> list[Path]:
    """场次文件（含已归档的 .jsonl.gz），按名字倒序 = 时间倒序。"""
    seen: dict[str, Path] = {}
    for pat in ("*.jsonl", "*.jsonl.gz"):
        for f in history_dir.glob(pat):
            # 同一个场次若两份都在（归档中途被打断），以未压缩的那份为准
            key = session_stem(f.name)
            if key not in seen or not f.name.endswith(".gz"):
                seen[key] = f
    return sorted(seen.values(), key=lambda p: session_stem(p.name),
                  reverse=True)


def _parse_frames(path: Path) -> tuple[dict, FrameStore]:
    """把场次 jsonl 解析成 header + FrameStore（列式，单遍流式读取）。

    ⚠️ 必须逐行解析。旧实现是 read_text() 再 split("\\n")：光把 244MB 读成
       21 万个字符串就先占了 500MB 峰值，列式省下来的内存又被还回去一半。

    🔴 整型列必须单独存（`array('q')`）。jsonl 里 `lap`/`gear`/`car_code` 是
       **整数**，若一律塞进 float64 列，读出来就变成 3.0 —— analyze_session
       输出的圈号会写成 3.0，`car_name_of(2181.0)` 更是直接查不到车型。
       列的类型按「该列是否只出现过整数」自动判定，遇到小数才升级为 double。
    """
    num: dict[str, array] = {}          # 标量列
    isint: dict[str, bool | None] = {}  # 该列是否整列都是整数（None = 还没见到非空值）
    arr: dict[str, list[array]] = {}    # 数组列（如 g_force）
    arrint: dict[str, list[bool | None]] = {}
    bl: dict[str, bytearray] = {}
    tbl: dict[str, list] = {}
    txi: dict[str, array] = {}
    tmap: dict[str, dict] = {}
    null: dict[str, list[int]] = {}
    absent: dict[str, list[int]] = {}
    fields: set[str] = set()
    header: dict = {}
    n = 0
    is_header = True

    def _track_type(flags, key: str, v) -> None:
        """记录「这一列到目前为止是不是全是整数」，解析完再据此定列类型。

        bool 不算整数（JSON 里 true/false 与 0/1 不是一回事）。
        一旦出现过小数就锁定为 False，不再翻转。
        """
        if isinstance(v, bool) or v is None:
            return
        if flags[key] is False:
            return
        if flags[key] is None:
            flags[key] = isinstance(v, int)

    with open_session(path) as fh:          # .jsonl.gz 归档也能读
        for line in fh:
            # 🔴 正在录制的场次：最后一行可能是**写了一半**的。
            #    记录器每帧 `fh.write(json.dumps(...) + "\n")`，所以
            #    「最后一行没有换行符」= 还没写完 → 必须丢掉。
            #    不丢的后果是灾难性的：`json.loads` 抛 JSONDecodeError，
            #    被 _load_frames 的 except 兜住之后返回 **空 store**，
            #    于是整场（20 万帧）在页面上显示成 0 帧、接口报「帧加载失败」。
            #    实测：末尾截断 60 字节 → frames 4183 → 0。
            if not line.endswith("\n"):
                break
            if not line.strip():
                continue
            if is_header:
                header = json.loads(line)
                is_header = False
                continue
            d = json.loads(line)
            fields |= d.keys()
            for k, kind in _FRAME_COL_KIND.items():
                v = d.get(k, _ABSENT)
                if v is _ABSENT:
                    absent.setdefault(k, []).append(n)
                if v is _ABSENT or v is None:
                    null.setdefault(k, []).append(n)
                    v = None
                if kind == "n":
                    if k not in num:
                        num[k] = array("d")
                        isint[k] = None
                    if v is None:
                        num[k].append(0.0)
                    else:
                        _track_type(isint, k, v)
                        num[k].append(float(v))
                elif kind == "a":
                    cols = arr.get(k)
                    flags = arrint.get(k)
                    vals = v if isinstance(v, list) else []
                    if cols is None:
                        cols = arr[k] = []
                        flags = arrint[k] = []
                    for j in range(len(cols), len(vals)):   # 列数由最长的帧决定
                        cols.append(array("d", [0.0] * n))  # 补齐已解析过的帧
                        flags.append(None)
                    for j, c in enumerate(cols):
                        x = vals[j] if j < len(vals) else None
                        if x is None:
                            c.append(0.0)
                        else:
                            _track_type(flags, j, x)
                            c.append(float(x))
                elif kind == "b":
                    bl.setdefault(k, bytearray()).append(1 if v else 0)
                else:  # 's'
                    tb = tbl.setdefault(k, [])
                    m = tmap.setdefault(k, {})
                    key = v if isinstance(v, str) else None
                    if key not in m:
                        m[key] = len(tb)
                        tb.append(key)
                    txi.setdefault(k, array("l")).append(m[key])
            n += 1

    if n == 0:
        return header, FrameStore.empty(header)
    # 整列都是整数的列改存 array('q')：否则 lap/gear/car_code 读出来是 3.0，
    # analyze_session 输出的圈号会写成 3.0，car_name_of(2181.0) 也查不到车型。
    # 解析时统一按 double 收，这里一次性转 —— 值都是整数，转换无损。
    for k, col in num.items():
        if isint.get(k) is True:
            num[k] = array("q", [int(x) for x in col])
    for k, cols in arr.items():
        flags = arrint.get(k) or []
        arr[k] = [array("q", [int(x) for x in c]) if flags[j] is True else c
                  for j, c in enumerate(cols)]
    # 整场都是 null / 整场都缺席的字段：用一个哨兵代替 21 万个下标
    nulls = {k: (_ALL if len(v) >= n else set(v)) for k, v in null.items()}
    absents = {k: (_ALL if len(v) >= n else set(v)) for k, v in absent.items()}
    return header, FrameStore(header, n, num, arr, bl, tbl, txi,
                              nulls, absents, fields)


def _load_frames(path: Path) -> tuple[dict, FrameStore]:
    """读场次 jsonl（header + 全部帧），带 2 条目缓存，返回列式 FrameStore。"""
    key = str(path)
    try:
        st = path.stat()
        sig = (st.st_mtime, st.st_size)
    except OSError:
        return {}, FrameStore.empty()
    with _CACHE_LOCK:
        hit = _FRAMES_CACHE.get(key)
        if hit and (hit[0], hit[1]) == sig:
            return hit[2], hit[3]
    try:
        header, store = _parse_frames(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return {}, FrameStore.empty()
    with _CACHE_LOCK:
        while len(_FRAMES_CACHE) >= 2:   # 只留最近 2 个场次，防内存膨胀
            _FRAMES_CACHE.pop(next(iter(_FRAMES_CACHE)))
        _FRAMES_CACHE[key] = (sig[0], sig[1], header, store)
    return header, store


def compare_session(path: Path, ref_lap_no: int | None = None,
                    cmp_lap_no: int | None = None) -> dict[str, Any]:
    """读场次 jsonl → 圈间对比分析（gt7analysis 纯函数库）。

    `ref_lap_no` 可选：参考圈号（赛车线 / 对比基准），缺省取最快圈。
    `cmp_lap_no` 可选：被对比的圈号，缺省取最后一圈。

    失败永远返回 {"error": ...} 而不是抛出——对比是增值功能，
    不能因为它挂掉影响详情页主体。
    """
    try:
        _, grouped, _ = _valid_laps(path)
        import gt7analysis
        # 赛车线抽稀在 gt7analysis.race_line 内部做（decimate=6）。
        # 🔴 不能在这里对每段各自 [::6]：分段后各自抽稀会把每段末尾
        #    到下一个边界的点丢掉，一圈上千个分色段留下上千个断隔。
        # 圈分组走 _valid_laps 的记忆化结果，不再自己跑一遍 clean_laps。
        r = gt7analysis.analyze_compare(None, ref_lap_no=ref_lap_no,
                                        cur_lap_no=cmp_lap_no, grouped=grouped)
        return r
    except Exception as e:
        return {"error": str(e)}


def race_line_session(path: Path, lap_no: int, decimate: int = 6) -> dict[str, Any]:
    """只算某一圈的赛车线（「行车轨迹」卡片切圈用）。

    为什么不复用 compare_session：那张卡要能独立换圈而不重算整页分析，
    整页 compare 的输出（时间差曲线 / 峰谷配对 / 圈速表）在这个场景里全是
    白算——一场 217k 帧的场次要几百毫秒。这里只做 clean_laps + 一圈采样。

    🔴 与 compare_session 共用 _load_frames 的解析缓存，不额外读盘。
    """
    try:
        _, grouped, _ = _valid_laps(path)
        import gt7analysis
        return gt7analysis.race_line_of_lap(None, int(lap_no),
                                            decimate=decimate, grouped=grouped)
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


# ---------------------------------------------------------------------------
# 录像 ↔ 遥测 时间轴锚点（POV 解说 / 自动剪辑的前置条件）
# ---------------------------------------------------------------------------
# jsonl 是不可变原始数据，录像绑定只能写边车（与收藏/改名同一先例）。
#
# 🔴 时间轴口径——三行必须一起看，弄反一个整段剪辑就错位：
#     t_session = 相对「本场第一个有效圈起点」的秒数（/highlights 给的就是它）
#     offset_s  = 录像开始时刻 − session_start（负数 = 录像比遥测早开始）
#     t_video   = t_session − offset_s
#   例：开跑前 18 秒就按了录制 → offset_s = −18 → 遥测第 30s 对应录像第 48s。
#   前端同时提供「录像比遥测早开始 N 秒」= −offset_s，免得用户心算正负号。


def lap_start_times(path: Path) -> dict[int, float]:
    """每圈首帧的墙上时钟（epoch 秒）。没有有效圈 → 空字典。"""
    try:
        _laps, grouped, _extra = _valid_laps(path)
    except Exception:
        return {}
    out: dict[int, float] = {}
    for ln, fs in grouped.items():
        if fs:
            t0 = fs[0].get("t")
            if t0:
                out[int(ln)] = float(t0)
    return out


def session_start_epoch(path: Path) -> float | None:
    """场次时间轴的零点 = 第一个有效圈的起点。None = 这场没有有效圈。"""
    m = lap_start_times(path)
    return min(m.values()) if m else None


def _video_entry(history_dir: Path, name: str) -> dict[str, Any]:
    canon = session_stem(name) + ".jsonl"
    return (load_session_meta(history_dir).get(canon) or {}).get("video") or {}


def probe_video_start(file: str) -> dict[str, Any]:
    """猜录像的开始时刻。

    🔴 mtime 陷阱：多数录制软件（OBS / PS5 相册 / 采集卡）的**文件修改时间
    是录完的时刻**，不是开始的时刻。所以优先用 ffprobe 拿时长反推：
        start = mtime − duration
    拿不到 ffprobe 就退回 mtime，并明确标注「偏晚整段时长」，不假装准确。
    """
    p = Path(file)
    if not p.exists():
        return {"ok": False,
                "reason": "文件不存在（dashboard 跑在服务器上时看不到你电脑里的文件，"
                          "属正常；请手动填开始时间或偏移）"}
    mtime = p.stat().st_mtime
    dur = None
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, timeout=15, text=True)
        if r.returncode == 0 and r.stdout.strip():
            dur = float(r.stdout.strip().splitlines()[0])
    except Exception:
        dur = None
    if dur and dur > 0:
        return {"ok": True, "start_epoch": mtime - dur, "source": "ffprobe",
                "duration_s": round(dur, 2), "mtime": mtime}
    return {"ok": True, "start_epoch": mtime, "source": "mtime",
            "warning": "没有 ffprobe，只能用文件修改时间；而多数录制软件的"
                       "修改时间是「录完」的时刻，会偏晚整段录像的时长"}


def video_session_info(history_dir: Path, path: Path) -> dict[str, Any]:
    """场次的录像绑定状态 + 换算好的锚点。"""
    start = session_start_epoch(path)
    iso = datetime.fromtimestamp(start).isoformat() if start else None
    b = _video_entry(history_dir, path.name)
    if not b.get("file"):
        return {"bound": False, "file": None, "offset_s": None,
                "session_start": round(start, 3) if start else None,
                "session_start_iso": iso,
                "formula": "t_video = t_session - offset_s",
                "hint": "未绑定录像。绑定后 /highlights 会额外给出每段高光"
                        "在录像里的秒数，ffmpeg 可直接切。"}
    off = float(b.get("offset_s") or 0.0)
    vs = (start + off) if start else None
    return {
        "bound": True,
        "file": b.get("file"),
        "offset_s": round(off, 3),
        # 「录像比遥测早开始多少秒」= −offset_s，正数是更直觉的读法
        "video_lead_s": round(-off, 3),
        "session_start": round(start, 3) if start else None,
        "session_start_iso": iso,
        "video_start_epoch": round(vs, 3) if vs else None,
        "video_start_iso": datetime.fromtimestamp(vs).isoformat() if vs else None,
        "source": b.get("source") or "manual",
        "bound_at": b.get("bound_at"),
        "formula": "t_video = t_session - offset_s",
    }


# 赛道库 data/tracks.json —— 赛道自动识别的持久化边车文件（jsonl 不可变，
# 所有标注都进边车，与 sessions_meta.json 同一先例）：
#   {
#     "next_id": 3,
#     "tracks": [ {"id": 1, "name": "赛道名", "desc": [[x,z]×200],
#                  "ref_len_m": 6937.8, "turns_cw": false,
#                  "created": "2026-10-08", "sample_session": "xxx.jsonl"} ],
#     "sessions": { "xxx.jsonl": {"track_id": 1, "dist": 0.006} }
#   }
# sessions 映射是**缓存**：场次第一次打开详情页时识别并落库，之后列表页
# 直接读映射展示赛道名，零开销（识别要解析 244MB jsonl，不能在列表页做）。
_TRACK_MATCH_TOL = 0.05
# 命中阈值。依据（7 个真实场次、6 条赛道的探针实测）：同赛道指纹 RMS 距离
# 0.0056，最近的不同赛道 0.2426 —— 间隔 43 倍。取 0.05：比同赛道值高一个
# 数量级（容下抽稀/丢包造成的形状抖动），离异赛道值还有 5 倍安全余量。


def load_tracks(history_dir: Path) -> dict[str, Any]:
    fp = history_dir / "tracks.json"
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"next_id": 1, "tracks": [], "sessions": {}}
        data.setdefault("next_id", 1)
        data.setdefault("tracks", [])
        data.setdefault("sessions", {})
        return data
    except Exception:
        return {"next_id": 1, "tracks": [], "sessions": {}}


def save_tracks(history_dir: Path, data: dict[str, Any]) -> None:
    fp = history_dir / "tracks.json"
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _first_car_code(path: Path) -> int:
    """轻量读场次首帧的 car_code（车型码，用于列表展示）。

    只读文件头几行，避免扫描整个 jsonl。首帧可能在菜单态
    （car_code 或为 0），此处尽力而为，没读到返回 0。
    """
    try:
        with open_session(path, "r") as fh:   # 归档后的 .jsonl.gz 一样读
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
    with _CACHE_LOCK:
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
        with open_session(path, "r") as fh:
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
    with _CACHE_LOCK:
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


# 归档用**最快档**：一场 250MB，level 9 要几十秒，而 level 1 的压缩比
# 只差一两个百分点（jsonl 的冗余主要在重复的键名上，一档就吃掉了）。
_ARCHIVE_LEVEL = 1
_ARCHIVE_CHUNK = 1 << 20


def archive_session(f: Path) -> bool:
    """把一场就地压缩成 `X.jsonl.gz`（无损），成功返回 True。

    🔴 先写 `.gz.tmp` 再 `os.replace`：压缩一场要几秒，中途崩了不能留下
       半截 .gz 让人以为归档成功了（那是**数据丢失**级别的错觉）。
    🔴 压完确认真的变小了才删原件 —— 否则宁可保持原样。
    🔴 删原件前把该场次的解析缓存清掉：缓存条目的键是老路径，留着会让
       后续请求去读一个已经不存在的文件。
    """
    f = Path(f)
    if f.name.endswith(".gz") or not f.exists():
        return False
    gz = Path(str(f) + ".gz")
    if gz.exists():
        return False
    tmp = Path(str(gz) + ".tmp")
    try:
        src_size = f.stat().st_size
        with f.open("rb") as src, \
                gzip.open(tmp, "wb", compresslevel=_ARCHIVE_LEVEL) as dst:
            shutil.copyfileobj(src, dst, _ARCHIVE_CHUNK)
        if not tmp.exists() or tmp.stat().st_size >= src_size:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, gz)
        with _CACHE_LOCK:
            _FRAMES_CACHE.pop(str(f), None)
            _FRAMES_CACHE.pop(str(gz), None)
        f.unlink()
        return True
    except (OSError, EOFError, gzip.BadGzipFile):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


_ARCHIVE_SWEEP_LOCK = threading.Lock()
_ARCHIVE_SWEEPING = False


def _kick_archive_sweep(history_dir: Path, live: bool) -> None:
    """在后台线程跑一次归档巡检（不阻塞列表页，也不并发跑第二遍）。"""
    global _ARCHIVE_SWEEPING
    days = load_settings(history_dir).get("archive_after_days", 1)
    try:
        days = float(days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0 or _ARCHIVE_SWEEPING:
        return

    def _run():
        global _ARCHIVE_SWEEPING
        try:
            sweep_archive(history_dir, days, live=live)
        finally:
            _ARCHIVE_SWEEPING = False

    _ARCHIVE_SWEEPING = True
    threading.Thread(target=_run, daemon=True).start()


def sweep_archive(history_dir: Path, days: float,
                  live: bool = False) -> int:
    """把超过 `days` 天没动过的场次压成 .jsonl.gz，返回本次归档了几场。

    🔴 正在录制的那场绝不碰（近 20s 内还有写入）：记录器还在往里追加，
       压出来是半截，而且 .jsonl 被删后记录器会写进一个不存在的文件。
    🔴 days <= 0 = 不自动归档（只想手动归档时用）。
    """
    if days <= 0:
        return 0
    cutoff = time.time() - days * 86400.0
    done = 0
    with _ARCHIVE_SWEEP_LOCK:
        for f in glob_sessions(history_dir):
            if f.name.endswith(".gz"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime > cutoff:
                continue
            if live and (time.time() - st.st_mtime) < 20.0:
                continue
            if archive_session(f):
                done += 1
    return done


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
    # 赛道识别结果（缓存映射）：只读 tracks.json 的 sessions 映射，
    # 🔴 绝不在列表页触发识别本身——那要解析整场 jsonl。
    _lib = load_tracks(history_dir)
    _track_names = {t["id"]: t.get("name") or "" for t in _lib["tracks"]}
    sessions = []
    # 是否正在录制（一次判定，循环内复用）——用于保护活场不被自动归档
    live = _recording_active(history_dir)
    now = time.time()
    # 归档（老场次压缩成 .jsonl.gz）在**后台**跑：压一场 250MB 要几秒，
    # 放进列表页的请求里会让页面卡住。
    _kick_archive_sweep(history_dir, live)
    for f in glob_sessions(history_dir)[:limit]:
        try:
            stat = f.stat()
            parts = session_stem(f.name).split("_", 2)
            # 🔴 收藏 / 改名 / 赛道库的键都是**未压缩**那个名字（X.jsonl）——
            #    归档只是换了后缀，用真名去查会查不到，收藏和自定义名就丢了。
            canon = session_stem(f.name) + ".jsonl"
            m = meta.get(canon) or meta.get(f.name) or {}
            import gt7analysis
            # 🔴 `car_code` 必须**也**出现在列表里：赛道工程师（Coach）用它做
            #    "是不是同一辆车"的判据 —— 数字相等即可，不依赖 `cars.csv`
            #    查表命中。只给 `car_name` 的话，车型表没命中时本场与候选场
            #    都是空串 → 过滤被跳过 → 静默跨车采用历史参考圈（拿慢车的
            #    最快圈去量快车，delta 会退化成一个恒定的 +8 秒）。
            car_code = _first_car_code(f)
            car_name = gt7analysis.car_name_of(car_code, csv_path)
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
                # 数字车型码：比车型名可靠（不经过 `cars.csv` 查表）。
                # 0 = 未知（老场次 / 解析失败），消费方要做"未知"处理。
                "car_code": car_code,
                "best_lap_s": best,
                "anomalous": anomalous,
                "time_str": time_str,
                # 已归档 = 磁盘上是 .jsonl.gz（无损，只是占地方小了 ~8 倍）
                "archived": f.name.endswith(".gz"),
                # 是否就是**正在录制**的那一场。
                # 外部消费者（赛道工程师）靠它挑出「当前这场」去取参考圈，
                # 否则只能靠 modified 猜——猜错就会拿上一场的参考圈去对齐这一场，
                # 而且是静默错。live 是场次级判断，所以同一时刻至多一条为 true。
                "live": bool(live and (now - stat.st_mtime) < 20.0),
                # —— 用户标注 ——
                "favorite": bool(m.get("favorite")),
                "custom_name": m.get("custom_name") or "",
            }
            # 赛道自动识别结果（缓存命中才有；未识别为空串）
            th = _lib["sessions"].get(canon) or _lib["sessions"].get(f.name) \
                or {}
            tid = th.get("track_id")
            entry["track_name"] = _track_names.get(tid, "") if tid else ""
            entry["track_id"] = tid if tid else None
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
        # 🔴 走 _valid_laps 而不是直接调 clean_laps：分组只取决于文件内容，
        #    而一次详情页渲染里 compare_session / race_line_session 要的是
        #    同一份分组。各自算一遍 = 把 21 万帧重扫两到三遍。
        _, laps, _ = _valid_laps(path)

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
# 逐帧遥测（场次详情页的「遥测数据」卡片 / CSV 导出）
# ---------------------------------------------------------------------------
# 接收器每帧落盘的字段有四十几个（速度/转速/档位/油门/刹车/G 力/四轮/油量…），
# 但详情页原先只把它汇总成几个数字，逐帧数据等于「存了看不见」。
# 这里把同一份帧换个粒度暴露出来：
#   · session_series —— 整场/单圈的**降采样**时序（画曲线用）
#   · session_frames —— **分页**逐帧表（翻页用，绝不整场塞进页面）
#   · session_csv    —— 全量 CSV（留给 Excel / pandas）
#
# 🔴 一律复用 _load_frames 的解析缓存，不再额外读盘：详情页本来就要读一遍
#    场次算统计，这里只是对同一份帧做抽稀/切片。
#    单场 jsonl 能到 250MB，再读第二遍纯属浪费。
_SERIES_COLS = ["t", "spd", "rpm", "thr", "brk", "gear", "glat", "glon", "fuel", "lap"]
# CSV 表头（与 _SERIES_COLS 逐位对应）。?cols= 也要能只出对应几列的表头。
_SERIES_HEAD = ["时间(s)", "速度(km/h)", "转速(rpm)", "油门(%)", "刹车(%)",
                "档位", "横向G", "纵向G", "油量(%)", "圈号"]


def _parse_cols(raw) -> tuple[list[int] | None, str | None]:
    """解析 `?cols=t,spd,rpm`，返回（要保留的列下标, 错误信息）。

    🔴 未知列名**直接报错**而不是静默忽略：调用方写错名字却拿到一份「少了
       几列」的数据，比拿到 400 难排查得多——它看起来是成功的。
    🔴 不传 / 空串 = 全列（向后兼容，前端自己就是这么用的）。
    """
    if not raw or not str(raw).strip():
        return None, None
    want = [c.strip() for c in str(raw).split(",") if c.strip()]
    if not want:
        return None, None
    idx = {c: i for i, c in enumerate(_SERIES_COLS)}
    bad = [c for c in want if c not in idx]
    if bad:
        return None, f"cols 里有未知列名：{', '.join(bad)}"
    return [idx[c] for c in want], None


def _pick(row: list, sel: list[int] | None) -> list:
    """按 sel 挑列。sel 为 None = 全列（不给 ?cols= 时的老行为）。"""
    return row if sel is None else [row[i] for i in sel]


def _fuel_pct(f: dict) -> float | None:
    """油量百分比。⚠️ 纯电车 gas_capacity=0，此时没有百分比口径，返回 None。"""
    cap = f.get("gas_capacity") or 0.0
    if not cap:
        return None
    return round((f.get("gas_level") or 0.0) / cap * 100.0, 1)


# GT7 的 u16 字段用 0xFFFF(65535) 表示「不适用」：菜单态会把
# lap / num_cars / quali_pos 一起写成 65535。这类值漏到表格、CSV 或公开 API 里
# 就是垃圾数据（实测一场 217k 帧的场次里有 452 帧处于这种状态，全在开跑前）。
# 前端有同款的 okPos() 过滤，后端这几处也必须自己挡一道。
_U16_SENTINEL = 65000


def _u16(v) -> int:
    """u16 字段的哨兵值归一：>= 65000 一律当 0（0 = 不适用）。"""
    n = int(v or 0)
    return 0 if n >= _U16_SENTINEL else n


def _lap_no(f: dict) -> int:
    """帧的圈号。菜单态哨兵值（0xFFFF=65535 等）归 0，避免表格/CSV 里冒出 65535。

    GT7 在菜单、自由练习等场景会把 lap 写成 0xFFFF；gt7analysis.clean_laps 已把
    这类圈整圈丢弃，但**逐帧**接口是按原始帧输出的，不归一化就会漏出来。
    统一按 0 处理（0 = 未进入计时圈），与前端「菜单态显示占位」的口径一致。
    """
    return _u16(f.get("lap"))


# 一帧里所有已知会用 0xFFFF 表示「不适用」的 u16 字段。
_U16_FIELDS = ("lap", "num_cars", "quali_pos")


def _normalize_u16_frame(f: dict) -> dict:
    """把一帧里的 u16 哨兵字段就地归一（返回同一个 dict，方便链式用）。

    🔴 只改**已存在**的键，不凭空补键 —— 否则「键缺席」会变成「键在但值为 0」，
       那些靠 `in` 判断字段有无的调用方口径就被改掉了。

    出口不止一个：/api/v1/live 有 _v1_live 自己挡一道，但 /api/state 的 `latest`
    是**原始帧**（直接透传接收器给的那一份），前端和第三方都能拿到，
    菜单态会直接显示「65535 辆车 / 第 65535 位」。所以在源头归一一次最省事。
    """
    for k in _U16_FIELDS:
        if k in f:
            f[k] = _u16(f.get(k))
    return f


def _series_cols(store: "FrameStore"):
    """把 _frame_row 要用的各通道底层列一次抓齐，之后每行只做下标。

    /csv 要给整场（实测 21 万帧）各出一行、每行取 10 个通道；表格分页走的
    是同一条路。逐帧走 Frame.get 每行多两层函数调用，整场下来在慢一点的
    机器上是几百毫秒。

    🔴 为什么绕开 Frame 直读列是安全的：_frame_row 里每个通道的兜底都是
       0.0（`f.get(k) or 0.0`），而列式存储在「键缺席 / 值为 null」时填的
       正是 0.0（见 _parse_frames 的 `num[k].append(0.0)`），两条路可观测
       结果一致。等价性由 tests/test_series_row.py 逐行锁死。

    返回 None 表示有通道列取不到。按现在的字段清单这不该发生（_parse_frames
    对 _FRAME_COL_KIND 里每个字段都无条件建列，缺值时填 0.0），所以它是一道
    「以后有人动了字段清单」的护栏 —— 一旦有人把某个字段从清单里摘掉，
    直读会静默给出全 0 的假数据，那比慢得多的问题严重。返回 None 时调用方
    退回逐帧的 _frame_row（它走 Frame 的通用取值路径，会显出真实语义）。
    """
    num = store._num
    axes = store._arr.get("g_force") or ()
    try:
        return (
            num["t"], num["speed_kph"], num["rpm"], num["throttle"],
            num["brake"], num["gear"], num["gas_level"], num["gas_capacity"],
            # g[0]=纵向、g[1]=横向（与 _frame_row 的取法一致）
            axes[0] if len(axes) > 0 else None,
            axes[1] if len(axes) > 1 else None,
            num["lap"],
        )
    except KeyError:
        return None


def _frame_row_at(cols, i: int, t0: float) -> list:
    """_frame_row 的按列直读版（列序与它逐字对应）。"""
    (t_c, sp_c, rpm_c, th_c, bk_c, gr_c, gl_c, gc_c,
     g_lon_c, g_lat_c, lap_c) = cols
    cap = gc_c[i]
    fuel = round(gl_c[i] / cap * 100.0, 1) if cap else None
    return [
        round(t_c[i] - t0, 3),
        round(sp_c[i], 1),
        int(round(rpm_c[i])),                        # 转速取整：曲线/表格都不要小数
        round(th_c[i] * 100, 0),
        round(bk_c[i] * 100, 0),
        int(gr_c[i]),
        round(g_lat_c[i] if g_lat_c is not None else 0.0, 2),   # 横向 G
        round(g_lon_c[i] if g_lon_c is not None else 0.0, 2),   # 纵向 G
        fuel,
        _u16(lap_c[i]),
    ]


def _row_builder(store: "FrameStore", t0: float):
    """返回 row(frame) -> list。能按列直读就给快路，否则退回逐帧实现。"""
    cols = _series_cols(store)
    if cols is None:
        return lambda f: _frame_row(f, t0)
    return lambda f: _frame_row_at(cols, f._i, t0)


def _frame_row(f: dict, t0: float) -> list:
    """把一帧压成一行（列顺序见 _SERIES_COLS）。

    走 Frame 的通用取值路径 —— 慢路，也是 _frame_row_at 的语义基准。
    """
    g = f.get("g_force") or [0.0, 0.0, 0.0]
    return [
        round((f.get("t") or 0.0) - t0, 3),
        round(f.get("speed_kph") or 0.0, 1),
        int(round(f.get("rpm") or 0.0)),           # 转速取整：曲线/表格都不需要小数
        round((f.get("throttle") or 0.0) * 100, 0),
        round((f.get("brake") or 0.0) * 100, 0),
        int(f.get("gear") or 0),
        round(g[1] if len(g) > 1 else 0.0, 2),      # 横向 G
        round(g[0] if len(g) > 0 else 0.0, 2),      # 纵向 G（正=加速，负=制动）
        _fuel_pct(f),
        _lap_no(f),
    ]


def _valid_laps(path: Path) -> tuple[list[dict], dict, float]:
    """场次的有效圈清单 + 按圈分组的帧 + 该场起始 t。

    圈口径复用 gt7analysis.clean_laps（与圈速表 / 赛车线 / 参考圈同一套），
    避免这里算一套、那边算一套导致圈号对不上。

    🔴 结果按场次记忆化。clean_laps 内部是「按 lap 分组 + 每圈算距离 +
       每圈算速度峰值」三遍 O(n) 逐帧取值；而它只取决于文件内容。
       详情页每个接口（/frames、/series、/csv、/raceline）都调这一支，
       一场 217k 帧的次次重算实测要多花 0.2s/次。

    传给 clean_laps 的是 lap_frames 而不是全部帧：split_laps 本来就按
    `lap <= 0` 丢弃，而缺 lap 字段的帧读出来正是 0，同样被丢——两份输入
    得到的分组逐圈完全相同，但省掉一次全量遍历，还让两者共用那批 Frame 对象。
    """
    _, store = _load_frames(path)
    if not store:
        return [], {}, 0.0
    def _compute():
        t0 = store[0].get("t") or 0.0
        import gt7analysis
        grouped = gt7analysis.clean_laps(store.lap_frames())
        laps = []
        for no, fs in sorted(grouped.items()):
            if len(fs) < 2:
                continue
            laps.append({
                "lap": no,
                "t0": round((fs[0].get("t") or 0.0) - t0, 3),
                "dur": round((fs[-1].get("t") or 0.0)
                             - (fs[0].get("t") or 0.0), 3),
                "frames": len(fs),
            })
        return (laps, grouped, t0)
    return store.memo_compute("valid_laps", _compute)


def _scope_t0(scope: list[dict], session_t0: float, lap_no: int | None) -> float:
    """时间基准：选了单圈就从**该圈起点**算，否则从场次起点算。

    🔴 曲线、表格、CSV 必须共用同一个基准。之前这里只让曲线减了圈首时间、
    表格仍在印场次绝对时间（第 3 圈第一帧显示 162.70s），同一圈在两张图里
    对不上，读「入弯在第几秒」会直接读错。
    """
    if lap_no and scope:
        return scope[0].get("t") or session_t0
    return session_t0


def session_series(path: Path, lap_no: int | None = None,
                   max_points: int = 2400,
                   sel: list[int] | None = None) -> dict[str, Any]:
    """整场（或指定一圈）的降采样时序 + 圈边界，供详情页画全通道曲线。

    抽稀用固定 stride 而不是简单截断——30 万帧的场次截前 2400 帧
    只能看到开场 40 秒，曲线完全没意义。

    `sel` = `?cols=` 挑出来的列下标（None = 全列）。
    """
    try:
        laps, grouped, sess_t0 = _valid_laps(path)
    except Exception as e:
        return {"error": str(e)}
    _, store = _load_frames(path)
    fr = store.lap_frames()
    if not fr:
        return {"error": "没有可用的帧（缺少 lap 字段）"}

    scope = fr
    if lap_no:
        scope = grouped.get(lap_no) or []
        if len(scope) < 2:
            return {"error": f"第 {lap_no} 圈没有可用数据"}
    t0 = _scope_t0(scope, sess_t0, lap_no)
    step = max(1, int(math.ceil(len(scope) / max(1, max_points))))
    row = _row_builder(store, t0)
    rows = [_pick(row(f), sel) for f in scope[::step]]
    # 抽稀会漏掉最后一帧，补上——否则曲线右端「差一截」，看着像数据断了
    if scope and (len(scope) - 1) % step:
        rows.append(_pick(row(scope[-1]), sel))
    return {
        "cols": _SERIES_COLS if sel is None else [_SERIES_COLS[i] for i in sel],
        "rows": rows,
        "lap": lap_no or 0,
        "laps": laps,
        # total_frames = 整场帧数（恒定）；scope_frames = 当前所选范围的帧数。
        # 前端展示必须用 scope_frames——否则选了「第 3 圈」还写着整场的帧数，
        # 用户会以为抽稀把圈数据搞丢了。
        "total_frames": len(fr),
        "scope_frames": len(scope),
        "sampled_frames": len(rows),
        "step": step,
    }


def session_profile(path: Path, lap_no: int | None = None,
                    step_m: float = 5.0,
                    prominence_kph: float = 12.0) -> dict[str, Any]:
    """一圈的「按赛道位置索引」剖面 —— 给**外部消费者**用的打包接口。

    为什么要有这个端点（而不是让消费方自己算）：
        赛道工程师（gt7-coach）要的是「一圈的几何折线 + 等距遥测 + 刹车点/弯心/
        给油点」。这些东西 here 已经有了（clean_laps / lap_samples /
        find_peaks_valleys / 距离重采样），消费方自己重写一遍不仅费力，还会
        因为实时侧只有 10Hz 轮询而**精度更差**、第一圈完全没有参考。所以一次性
        打包发出去，谁都不用重复建图。

    🔴 距离轴是**几何弧长**（car_x/car_z 相邻弦长累积），不是 /series 的速度积分。
       速度积分一圈漂移 60~300 m（实测），拿它做「本圈 1200 m vs 参考圈 1200 m」
       对齐，实际赛道上能差十几米 —— 实时刹车点预告会直接指错位置。
       两种口径都返回（length_m / length_by_speed_m），差值当诊断指标。

    🔴 **正在跑的那一圈不能当参考圈。** 一场正在录制的比赛里，圈号最大的那一圈
       还在跑：它时长更短（所以 clean_laps 的速度积分口径下反而"最快"）、
       坐标折线只覆盖半条赛道。拿它做参考，实时最近点定位会大面积失配 ——
       实测横向误差飙到 300 m（≈车在 5 秒里跑过的距离），
       而错误表现是"偶尔算错"而不是"报错"，极难查。
       所以录制进行中时，把最大圈号排除在外。

    `lap` 缺省 = 最快圈（与 /raceline 同一约定），但只在**已跑完**的圈里挑。
    """
    try:
        laps, grouped, _t0 = _valid_laps(path)
    except Exception as e:
        return {"error": str(e)}
    if not grouped:
        return {"error": "没有可用的圈数据"}

    _, store = _load_frames(path)
    if not store:
        return {"error": "帧加载失败"}

    # 录制进行中 → 排除「正在跑的那一圈」。
    # 🔴 判据必须取**原始帧里的最大圈号**，不能用 clean_laps 过滤后的最大圈号：
    #    半圈常常已经被 clean_laps 当假圈剔掉了，那时 filtered 的最大圈号是一个
    #    **跑完了的**合法圈 —— 拿它去排除等于白白丢掉一份好参考
    #    （实测：一圈完整的第 2 圈被误排除，参考圈退回到第 1 圈）。
    recording = _recording_active(path.parent)
    # 🔴 「正在跑的那一圈」的圈号要先算出来，而且要让**调用方也看得见**（见下面
    #    meta.in_progress）。原因：本场只有一圈时，上面的兜底 `trimmed or usable`
    #    会退回把**半圈**当参考发出去 —— 那一刻 available_laps / recording 与
    #    "已经跑完一圈"时**长得一模一样**，消费方（赛道工程师）光看
    #    "有没有拿到 profile"根本分不出来，只能拿半圈的折线去做最近点定位。
    #    所以这里明说一句，把判断权交回去。
    raw_max = store.memo_compute(
        "raw_max_lap",
        lambda: max((f.get("lap") or 0) for f in store.lap_frames()))
    in_progress_lap = raw_max if recording else None
    usable = sorted(grouped.keys())
    if recording:
        trimmed = [n for n in usable if n != raw_max]
        # 不能把唯一的一圈也排掉，否则永远没有参考圈可用
        usable = trimmed or usable

    if lap_no is not None and int(lap_no) not in usable:
        want = int(lap_no)
        # 两种拒绝理由要分清：正在跑 vs 根本没有这一圈。
        # 混成一句话会让消费方无从下手（"重试"还是"别重试"）。
        in_progress = in_progress_lap is not None and want >= in_progress_lap
        why = "lap_in_progress" if in_progress else "no_data"
        return {"error": f"第 {lap_no} 圈不可作为参考（{why}）",
                "why": why, "recording": recording,
                "in_progress_lap": in_progress_lap,
                "available_laps": usable}

    if lap_no is None:
        if not usable:
            return {"error": "还没有跑完一整圈，暂无参考圈", "why": "no_lap",
                    "recording": recording, "available_laps": [],
                    "in_progress_lap": in_progress_lap}
        best = (analyze_session(path).get("best_lap") or {}).get("lap")
        lap_no = best if best in usable else usable[-1]

    key = ("profile", int(lap_no), round(float(step_m), 2),
           round(float(prominence_kph), 2),
           # 🔴 「录制中吗 / 正在跑第几圈」也必须进 key。
           #    `meta`（含 `in_progress`）是在 `_compute` **里面**拼的，而
           #    `_compute` 的结果整体进记忆化 —— 不把这两个状态量算进 key，
           #    就会出现「文件没变、录制刚结束」时把上一次的 meta 原样发回去：
           #    `in_progress` 永远停在 true，消费方（赛道工程师）于是一直
           #    不敢用这份参考圈，而文件内容明明早就跑完了。
           recording, in_progress_lap)

    def _compute() -> dict:
        import gt7analysis
        prof = gt7analysis.lap_profile(
            grouped[int(lap_no)], lap_no=int(lap_no), step_m=step_m,
            prominence_kph=prominence_kph)
        # 🔴 整个响应体一起进记忆化，而不是只在里面存 lap_profile 的输出、
        #    外面再拼一层 meta：那样每次请求都会新建一个 dict，
        #    「命中缓存」这件事就变得测不出来（内容相等但对象不同）。
        out: dict[str, Any] = {
            "meta": {"api_version": 1, "file": path.name,
                     "available_laps": usable,
                     "recording": recording,
                     # 🔴 返回给你的这一圈**还在跑**吗？只有"本场唯一一圈"
                     #    （`trimmed or usable` 的兜底分支）才可能为真。
                     #    消费方必须据此拒绝它：半圈的折线只覆盖半条赛道，
                     #    圈长还随车前进一直变（实测 5491 m → 7493 m）。
                     "in_progress": bool(in_progress_lap is not None
                                         and lap_no == in_progress_lap),
                     "in_progress_lap": in_progress_lap,
                     "laps": laps},
        }
        out.update(prof)
        return out

    # 🔴 返回的是**记忆化对象本身**，调用方一律当只读用（处理器只做 JSON 序列化）。
    return store.memo_compute(key, _compute)


def session_frames(path: Path, offset: int = 0, limit: int = 200,
                   lap_no: int | None = None,
                   sel: list[int] | None = None) -> dict[str, Any]:
    """逐帧数据分页。limit 硬上限 1000——别让人一页拉爆浏览器。"""
    try:
        _, grouped, sess_t0 = _valid_laps(path)
    except Exception as e:
        return {"error": str(e)}
    _, store = _load_frames(path)
    fr = (grouped.get(lap_no) or []) if lap_no else store.lap_frames()
    t0 = _scope_t0(fr, sess_t0, lap_no)
    total = len(fr)
    offset = max(0, int(offset or 0))
    limit = max(1, min(int(limit or 200), 1000))
    page = fr[offset:offset + limit]
    row = _row_builder(store, t0)
    return {
        "cols": _SERIES_COLS if sel is None else [_SERIES_COLS[i] for i in sel],
        "rows": [_pick(row(f), sel) for f in page],
        "offset": offset, "limit": limit, "total": total,
        "lap": lap_no or 0,
    }


def session_csv(path: Path, lap_no: int | None = None,
                sel: list[int] | None = None) -> str:
    """逐帧数据导成 CSV（Excel / pandas 可直接打开）。"""
    _, grouped, sess_t0 = _valid_laps(path)
    _, store = _load_frames(path)
    fr = (grouped.get(lap_no) or []) if lap_no else store.lap_frames()
    t0 = _scope_t0(fr, sess_t0, lap_no)
    head = (_SERIES_HEAD if sel is None
            else [_SERIES_HEAD[i] for i in sel])
    out = [",".join(head)]
    row = _row_builder(store, t0)
    for f in fr:
        r = _pick(row(f), sel)
        out.append(",".join("" if v is None else str(v) for v in r))
    return "\n".join(out) + "\n"


def session_sectors(path: Path, n_sectors: int = 4) -> dict[str, Any]:
    """分段计时 / 理论最快圈。

    分段边界按**距离**等分（不是时间等分）—— 只有把边界钉在同一段路上，
    跨圈的段用时才可比。理论最快圈 = 各段全场最小用时之和。

    🔴 结果按场次记忆化。本场 217k 帧实测 0.39s，而它只取决于文件内容；
       详情页那张卡片每次展开都调它，不记忆就是纯浪费。

    🔴 异常圈过滤在 gt7analysis.sector_times 里做，别在这里"优化"掉：
       不过滤会得到 +8.48% 的假空间，过滤后才是真实的 +0.71%。
    """
    n_sectors = max(2, min(int(n_sectors or 4), 10))
    try:
        _, grouped, _ = _valid_laps(path)
    except Exception as e:
        return {"error": str(e), "laps": [], "reliable": False}
    _, store = _load_frames(path)
    if not store:
        return {"error": "no frames", "laps": [], "reliable": False}
    def _compute():
        import gt7analysis
        return gt7analysis.sector_times(grouped, n_sectors=n_sectors)
    return store.memo_compute(("sectors", n_sectors), _compute)


def session_slip(path: Path, max_per_lap: int = 120) -> dict[str, Any]:
    """轮胎滑移（空转 / 抱死）检测，基于四轮角速度。

    半径自标定、前后轴分别标定；缺 wheel_rads 的老场次返回 available=False
    （前端据此整卡隐藏，不要显示空表）。

    🔴 结果按场次记忆化。本场实测 0.53s。注意它比 /sectors 更贵，因为要
       逐帧走一遍全部帧；而返回的 series 体积由**圈数**封顶（23×120 点），
       不随帧数膨胀。
    """
    max_per_lap = max(20, min(int(max_per_lap or 120), 600))
    try:
        _, grouped, _ = _valid_laps(path)
    except Exception as e:
        return {"available": False, "reason": str(e)}
    _, store = _load_frames(path)
    if not store:
        return {"available": False, "reason": "no frames"}
    def _compute():
        import gt7analysis
        return gt7analysis.wheel_slip(grouped, max_per_lap=max_per_lap)
    return store.memo_compute(("slip", max_per_lap), _compute)


def session_deviation(path: Path, ref_lap: int | None = None,
                      cmp_lap: int | None = None,
                      step: float = 5.0) -> dict[str, Any]:
    """走线偏差（横向偏移热力图）：本圈相对参考圈的逐米 dlat 通道。

    `ref_lap` 缺省取最快圈，`cmp_lap` 缺省取「非 ref 的另一有效圈」——
    与「圈间对比」卡片的口径一致。两圈不存在或其一不达标（出场圈 / 残圈
    / 距离对齐失败）时会返回 `reliable=false` + `reason`，前端整段画灰底
    说明而不吐错。

    🔴 结果按场次记忆化。本场 217k 帧实测 ~80ms（一次最贵的最近点搜索
       一次性跑完），命中缓存 0ms。同一份 (ref, cmp, step) 在同一场次里
       反复切换时才不会被反复算。
    """
    step = max(1.0, min(float(step or 5.0), 50.0))
    try:
        laps_list, grouped, _ = _valid_laps(path)
    except Exception as e:
        return {"error": str(e), "reliable": False, "line": [], "grid_m": [],
                "segments": [], "ref_line": []}
    if not grouped:
        return {"error": "no frames", "reliable": False, "line": [],
                "grid_m": [], "segments": [], "ref_line": []}
    valid_nos = sorted(grouped.keys())
    if not valid_nos:
        return {"error": "no valid lap", "reliable": False, "line": [],
                "grid_m": [], "segments": [], "ref_line": []}

    # 参考圈缺省 = 最快圈；与「圈间对比 / 行车轨迹」卡片共用同一选择逻辑。
    if ref_lap is None or ref_lap not in grouped or len(grouped[ref_lap]) < 10:
        best = (analyze_session(path) or {}).get("best_lap") or {}
        ref_lap = best.get("lap") if best.get("lap") in grouped else valid_nos[0]
    # 对比圈缺省 = 最后一圈（圈号最大的有效圈），与「圈间对比」卡片
    # 共用同一口径。
    if cmp_lap is None:
        cands = sorted(grouped.keys(), reverse=True)
        cmp_lap = next((n for n in cands if n != ref_lap), None)
        if cmp_lap is None:
            cmp_lap = valid_nos[-1]
    # 显式指定：ref 与 cmp 撞成同一圈、或圈不在 grouped 里 —— 直接报错，
    # 而不是悄悄换号。悄悄换号会让「我点了 6 号没反应」的排查很难。
    if cmp_lap not in grouped:
        return {"error": f"对比圈 {cmp_lap} 不在场次里",
                "reliable": False, "line": [], "grid_m": [],
                "segments": [], "ref_line": []}
    if ref_lap == cmp_lap:
        return {"error": "ref 与 cmp 撞成同一圈", "reliable": False,
                "line": [], "grid_m": [], "segments": [], "ref_line": []}

    _, store = _load_frames(path)
    if not store:
        return {"error": "no frames", "reliable": False, "line": [],
                "grid_m": [], "segments": [], "ref_line": []}
    def _compute():
        import gt7analysis
        return gt7analysis.track_deviation(
            gt7analysis.lap_samples(grouped[int(ref_lap)]),
            gt7analysis.lap_samples(grouped[int(cmp_lap)]),
            step=step)
    hit = store.memo_compute(("deviation", int(ref_lap), int(cmp_lap), step),
                             _compute)
    # 每次返回都补这两个字段（让前端切圈时知道参考圈 / 对比圈当前是几号），
    # 同时把可圈清单给前端做下拉用。
    hit = dict(hit)
    hit["ref_lap"] = int(ref_lap)
    hit["cmp_lap"] = int(cmp_lap)
    hit["laps_list"] = [int(x["lap"]) for x in laps_list]
    return hit


def session_track(path: Path) -> dict[str, Any]:
    """赛道自动识别：本场的形状指纹 ↔ 赛道库（data/tracks.json）匹配。

    - 已在库映射里 ⇒ 直接返回缓存结果（不解析 jsonl，列表页也可安全调用
      带映射的查询路径）。
    - 首次见到 ⇒ 算指纹（记忆化到 store._memo）→ 与库内每条比 RMS 距离 →
      命中(_TRACK_MATCH_TOL 内)则挂靠该赛道；否则**新建一条未命名赛道**。
      结果写回 tracks.json，之后的加载走快路径。

    🔴 指纹本身只取决于文件内容，纯记忆化；但「匹配 + 落库」有副作用，
       所以副作用只在未映射时发生一次，幂等。
    """
    hist = path.parent
    lib = load_tracks(hist)
    name = path.name

    # —— 快路径：已识别过 ——
    hit = lib["sessions"].get(name)
    if hit:
        tr = next((t for t in lib["tracks"] if t["id"] == hit.get("track_id")),
                  None)
        if tr:
            return {"track_id": tr["id"], "name": tr["name"],
                    "matched": bool(tr["name"]), "distance": hit.get("dist"),
                    "ref_len_m": tr.get("ref_len_m")}

    # —— 慢路径：算指纹并匹配 ——
    try:
        _, grouped, _ = _valid_laps(path)
    except Exception as e:
        return {"error": str(e)}
    _, store = _load_frames(path)
    if not store:
        return {"error": "no frames"}
    def _compute():
        import gt7analysis
        return gt7analysis.track_fingerprint(grouped)
    fp = store.memo_compute(("fingerprint",), _compute)
    if "error" in fp:
        # 没有可用圈：不落库，也不报成页面错误——识别是增值功能
        return {"error": fp["error"], "identified": False}

    import gt7analysis
    best_id, best_d = None, None
    for tr in lib["tracks"]:
        d = gt7analysis.fingerprint_distance(fp["desc"], tr["desc"])
        if best_d is None or d < best_d:
            best_id, best_d = tr["id"], d

    if best_d is not None and best_d <= _TRACK_MATCH_TOL:
        track_id, track_name, matched = best_id, next(
            t["name"] for t in lib["tracks"] if t["id"] == best_id), True
    else:
        # 新建未命名赛道。名字留空，UI 显示「未命名赛道 #N」并给改名入口。
        track_id = int(lib["next_id"])
        lib["next_id"] = track_id + 1
        track_name, matched = "", False
        lib["tracks"].append({
            "id": track_id, "name": track_name,
            "desc": fp["desc"], "ref_len_m": fp["ref_len_m"],
            "turns_cw": fp["turns_cw"],
            "created": datetime.now().strftime("%Y-%m-%d"),
            "sample_session": name,
        })
    lib["sessions"][name] = {"track_id": track_id, "dist": round(best_d, 4)
                             if best_d is not None else None}
    try:
        save_tracks(hist, lib)
    except OSError:
        pass    # 写不进就当会话内缓存用，下次再试
    return {"track_id": track_id, "name": track_name, "matched": matched,
            "distance": lib["sessions"][name]["dist"],
            "ref_len_m": fp["ref_len_m"]}


# ---------------------------------------------------------------------------
# 驾驶事件时间线（gt7-event-detector.py 适配层）
# ---------------------------------------------------------------------------

_DETECTOR_MOD: Any = None
_DETECTOR_TRIED = False

_EVENT_TYPE_NAMES: dict[str, str] = {
    "hard_braking": "极限刹车", "collision": "碰撞", "spin": "打滑/失控",
    "tyre_abuse": "轮胎滥用", "heavy_throttle": "大油门出弯",
    "off_track": "出界",
}


def _load_detector():
    """importlib 加载 gt7-event-detector.py（文件名带连字符，不能 import）。

    🔴 exec_module 前必须先 sys.modules[name] = mod：模块里有 @dataclass，
       dataclass 处理 `X | None` 注解时会回头查 sys.modules，不注册直接
       AttributeError。文件缺失/加载失败返回 None（部署目录可裁剪该文件），
       事件卡显示「不可用」而不是 500。
    """
    global _DETECTOR_MOD, _DETECTOR_TRIED
    if _DETECTOR_TRIED:
        return _DETECTOR_MOD
    _DETECTOR_TRIED = True
    p = Path(__file__).with_name("gt7-event-detector.py")
    if not p.exists():
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "gt7_event_detector", p)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["gt7_event_detector"] = mod
        spec.loader.exec_module(mod)
        _DETECTOR_MOD = mod
    except Exception:
        _DETECTOR_MOD = None
    return _DETECTOR_MOD


def _frame_to_sample(det, f, i: int):
    """Frame → 检测器 Sample。只映射检测器真正会读的字段。

    🔴 要读的逐帧字段必须先在 _FRAME_COL_KIND 里登记，否则 FrameStore
       会把「读了但没存」记进 MISSED_FIELDS，tests/test_frame_store.py 直接失败。
       tyre_temp / susp_height 已登记（实测有真值），所以这里读真值；
       steer_angle 协议里没有、tyre_press / tyre_wear 实测恒 0，三者都不读
       （留给 Sample 的默认值 0，避免把假数字带进解说词）。
    """
    # 转弯曲率 κ = a_lat / v²（正 = 左转）。
    # 这是**算出来的**不是协议字段：GT7 格式 A 不广播转向角，而事件的
    # 「往哪拐 / 拐多急」没有它会变成恒 0 的假数据。
    # 精度实测：与轨迹几何曲率相关 0.986，误差中位 0.0004 1/m。
    g = f.get("g_force") or (0.0, 0.0, 0.0)
    v_ms = (f.get("speed_kph") or 0.0) / 3.6
    curv = 0.0
    if v_ms > 2.0:                      # 低速时分母太小，κ 会飙到无意义
        curv = float(g[1]) * 9.80665 / (v_ms * v_ms)
    return det.Sample(
        t=f.get("t") or 0.0, seq=i,
        speed_kph=f.get("speed_kph") or 0.0,
        rpm=f.get("rpm") or 0.0,
        gear=int(f.get("gear") or 0),
        lap_count=int(f.get("lap") or 0),
        throttle=f.get("throttle") or 0.0,
        brake=f.get("brake") or 0.0,
        wheel_speed=list(f.get("wheel_rads") or (0.0, 0.0, 0.0, 0.0)),
        g_force=list(g),
        tyre_temp=list(f.get("tyre_temp") or (0.0, 0.0, 0.0, 0.0)),
        susp_height=list(f.get("susp_height") or (0.0, 0.0, 0.0, 0.0)),
        curvature=curv,
    )


def _dist_to_t(pts: list[dict], dist: float) -> float:
    """累计距离 → 该圈帧时间（线性插值；超界取端点）。

    lap_samples 的 pts 与圈帧一一对应，dist 单调（dt 异常区间不计距离，
    会持平——bisect 用右端点即可）。
    """
    import bisect
    ds = [p["dist"] for p in pts]
    k = min(bisect.bisect_left(ds, dist), len(pts) - 1)
    if k <= 0:
        return pts[0]["t"]
    d0, d1 = ds[k - 1], ds[k]
    if d1 <= d0:
        return pts[k]["t"]
    frac = (dist - d0) / (d1 - d0)
    return pts[k - 1]["t"] + (pts[k]["t"] - pts[k - 1]["t"]) * frac


def _events_compute(det, path: Path, grouped: dict, store: "FrameStore",
                    lap_no: int | None) -> dict[str, Any]:
    """session_events 的慢路径（结果由调用方记忆化）。"""
    import gt7analysis

    # —— 标定半径（与 /slip 共用一次标定，记忆化到同一份 memo）——
    calib = store.memo_compute(
        ("radii",), lambda: gt7analysis.calibrate_wheel_radii(grouped))
    radii = None
    if calib.get("available"):
        radii = (calib["front_m"], calib["rear_m"])
    detector = det.EventDetector(radii=radii)

    # 参考圈 = 最快圈（与「圈间对比 / 走线偏差」卡片同一口径，只算一次）
    best_lap = None
    stats = analyze_session(path)
    best = (stats.get("best_lap") or {}) if stats else {}
    if best.get("lap") in grouped:
        best_lap = int(best["lap"])

    lap_nos = sorted(grouped.keys()) if lap_no is None else (
        [lap_no] if lap_no in grouped else [])
    events: list[dict[str, Any]] = []
    for ln in lap_nos:
        fs = grouped[ln]
        if len(fs) < 2:
            continue
        samples = [_frame_to_sample(det, f, i) for i, f in enumerate(fs)]
        t0 = samples[0].t
        # 滑移 / 碰撞 / 刹车 / 大油门类。不走 detect_offtrack：
        # OFF_TRACK 由走线偏差主导（见下），检测器自带的「低速+高滑移」
        # 判据只在偏差不可信时兜底。
        evs = (detector.detect_impact(samples)
               + detector.detect_spin(samples)
               + detector.detect_hard_braking(samples)
               + detector.detect_tyre_abuse(samples)
               + detector.detect_heavy_throttle(samples))
        for e in evs:
            events.append({
                "lap": ln, "t_rel": round(e.t_start - t0, 3),
                "t_end_rel": round(e.t_end - t0, 3),
                "type": e.type.value,
                "confidence": round(e.confidence, 3),
                "evidence": e.evidence, "hint": e.comment_hint,
                "src": "telemetry",
            })
        # —— OFF_TRACK：|dlat|>5m 的持续段（session_deviation 的 memo 复用，
        #    走线偏差卡片看过同一圈就 0ms）——
        dev = session_deviation(path, ref_lap=best_lap, cmp_lap=ln)
        if dev.get("reliable"):
            pts = gt7analysis.lap_samples(fs)
            for run in dev.get("offtrack_runs") or []:
                a = _dist_to_t(pts, run["from_dist_m"])
                b = _dist_to_t(pts, run["to_dist_m"])
                events.append({
                    "lap": ln, "t_rel": round(a - t0, 3),
                    "t_end_rel": round(b - t0, 3),
                    "type": "off_track",
                    "confidence": min(1.0, run["max_abs_dlat"] / 15.0),
                    "evidence": {
                        "最大横向偏移_m": run["max_abs_dlat"],
                        "赛道位置_m": run["from_s_m"],
                    },
                    "hint": "车辆驶出赛道表面，抓地力大幅下降",
                    "src": "geometry",
                })
        else:
            for e in detector.detect_offtrack(samples):
                events.append({
                    "lap": ln, "t_rel": round(e.t_start - t0, 3),
                    "t_end_rel": round(e.t_end - t0, 3),
                    "type": e.type.value,
                    "confidence": round(e.confidence, 3),
                    "evidence": e.evidence, "hint": e.comment_hint,
                    "src": "slip",
                })
    events.sort(key=lambda e: (e["lap"], e["t_rel"]))
    # 每圈时长（秒）：前端给时间条定刻度用（条的全宽 = 该圈时长）
    lap_durs = {int(ln): round(fs[-1].get("t") - fs[0].get("t"), 3)
                for ln, fs in grouped.items() if len(fs) >= 2}
    calib_out = {k: calib.get(k) for k in
                 ("available", "front_m", "rear_m", "ratio", "ok", "reason")}
    if calib_out.get("front_m") is not None:
        calib_out["front_m"] = round(calib_out["front_m"], 4)
        calib_out["rear_m"] = round(calib_out["rear_m"], 4)
    return {
        "available": True,
        "calibration": calib_out,
        "laps": (sorted(int(n) for n in grouped.keys())
                 if lap_no is None else [int(lap_no)]),
        "lap_durs": lap_durs,
        "lap_scope": int(lap_no) if lap_no else 0,
        "events": events,
        "type_names": _EVENT_TYPE_NAMES,
        # 阈值透明化：API 消费方要知道事件是按什么门槛判出来的
        "thresholds": dict(vars(detector.th)),
    }


def session_events(path: Path, lap_no: int | None = None) -> dict[str, Any]:
    """驾驶事件时间线：打滑 / 碰撞 / 极限刹车 / 轮胎滥用 / 大油门 / 出界。

    口径与数据源：
      · 圈分组与圈速表 / 赛车线共用 _valid_laps（clean_laps）——检测器
        自带的 find_lap_boundaries 不剔假圈，**别用它**。
      · 滑移率用前后轴分标定半径（calibrate_wheel_radii）；不注入时整体
        偏置 ~1.3%，而抱死信号本身就只有百分之几。
      · 🔴 OFF_TRACK 由走线偏差主导（|dlat|>5m 持续段）：压草地未必滑移
        大，高速冲出弯也出界。偏差不可信（残圈 / 对齐失败）时才退回
        检测器的「低速+高滑移」判据（事件带 src 区分）。
      · 🔴 结果按场次记忆化。冷路径要逐帧构造 21 万个 Sample 并各跑一遍
        滑移检测（实测 ~10s），所以事件卡**展开时才取**；命中 0ms。
    """
    det = _load_detector()
    if det is None:
        return {"available": False,
                "reason": "gt7-event-detector.py 不在部署目录"}
    try:
        laps_list, grouped, _ = _valid_laps(path)
    except Exception as e:
        return {"available": False, "reason": str(e)}
    _, store = _load_frames(path)
    if not store:
        return {"available": False, "reason": "no frames"}
    hit = store.memo_compute(
        ("events", lap_no), lambda: _events_compute(det, path, grouped, store,
                                                    lap_no))
    hit = dict(hit)
    hit["laps_list"] = [int(x["lap"]) for x in laps_list]
    return hit


# ---------------------------------------------------------------------------
# 集锦高光（解说工具 Phase 1 的剪辑时间轴）
# ---------------------------------------------------------------------------

# 类型权重：与检测器 run_all() 里的 priority 一致（越"戏剧性"越该进集锦）。
_HIGHLIGHT_WEIGHT: dict[str, float] = {
    "collision": 10.0, "spin": 8.0, "off_track": 7.0,
    "tyre_abuse": 5.0, "heavy_throttle": 4.0, "hard_braking": 3.0,
}
_DEFAULT_WEIGHT = 1.0


def session_highlights(path: Path, top: int = 10, pad_before: float = 2.0,
                       pad_after: float = 1.5, min_score: float = 0.0,
                       types: list[str] | None = None,
                       lap_no: int | None = None,
                       history_dir: Path | None = None) -> dict[str, Any]:
    """把驾驶事件排成**可直接切片的集锦时间轴**。

    这是 POV 解说工具 Phase 1（集锦自动剪辑）的交接面：
    每个片段都给出相对场次起点的秒数，ffmpeg 直接 `-ss clip_start -t duration`
    就能切出来，不用再算一次时间轴。

    传了 `history_dir` 且该场次**已绑定录像**（见 `video_session_info`），
    每段还会多给 `clip_start_video` —— 录像时间轴上的秒数，省得消费方
    自己减一遍 offset（减错方向是这类对接最常见的翻车点）。

    score = confidence × 类型权重（碰撞 10 > 打滑 8 > 出界 7 > …）：
    置信度是「这个事件判得有多准」，权重是「这个事件值不值得放进集锦」，
    两者相乘才是排片顺序。

    🔴 clip 窗口会向两侧各留 pad 秒（默认前 2s / 后 1.5s）——切片不能
       从事件正中间开始，否则观众看不到"怎么发生的"。
    """
    ev = session_events(path, lap_no=lap_no)
    if not ev.get("available"):
        return {"available": False, "reason": ev.get("reason", "events unavailable"),
                "clips": []}

    # 圈起点绝对时间（墙上时钟）：把 t_rel 还原成整场时间轴要它。
    lap_t0 = lap_start_times(path)
    if not lap_t0:
        return {"available": False, "reason": "no lap timestamps", "clips": []}
    session_start = min(lap_t0.values())

    # 录像锚点（可为空）：t_video = t_session − offset_s
    vinfo = (video_session_info(history_dir, path)
             if history_dir is not None else None)
    voff = vinfo.get("offset_s") if (vinfo and vinfo.get("bound")) else None

    tset = {t.strip() for t in types} if types else None
    clips = []
    for e in ev.get("events") or []:
        etype = e.get("type") or ""
        if tset and etype not in tset:
            continue
        conf = float(e.get("confidence") or 0.0)
        score = conf * _HIGHLIGHT_WEIGHT.get(etype, _DEFAULT_WEIGHT)
        if score < min_score:
            continue
        t0 = lap_t0.get(int(e.get("lap") or 0))
        if t0 is None:
            continue
        t_abs = t0 - session_start + float(e.get("t_rel") or 0.0)
        t_end_abs = t0 - session_start + float(
            e.get("t_end_rel") or e.get("t_rel") or 0.0)
        start = max(0.0, t_abs - pad_before)
        end = t_end_abs + pad_after
        clips.append({
            "type": etype,
            "type_cn": _EVENT_TYPE_NAMES.get(etype, etype),
            "lap": e.get("lap"),
            "confidence": round(conf, 3),
            "score": round(score, 3),
            # 事件本身（相对场次起点，秒）
            "t_start": round(t_abs, 3), "t_end": round(t_end_abs, 3),
        # 切片窗口（含留白，ffmpeg 直接用这个）
        "clip_start": round(start, 3),
        "clip_end": round(end, 3),
        "duration": round(max(0.1, end - start), 3),
        # 录像时间轴（绑了录像才有）：t_video = t_session − offset_s
        "clip_start_video": (round(start - voff, 3) if voff is not None else None),
        "clip_end_video": (round(end - voff, 3) if voff is not None else None),
            "evidence": e.get("evidence") or {},
            "hint": e.get("hint") or "",
            "src": e.get("src") or "",
        })
    clips.sort(key=lambda c: -c["score"])
    if top and top > 0:
        clips = clips[:top]
    for i, c in enumerate(clips, 1):
        c["rank"] = i
    return {
        "available": True,
        "session_start": round(session_start, 3),
        # iso 用**本地时区**：这是给人核对"这段对应录像第几秒"用的，
        # 而录像文件的创建时间也是本地时间，UTC 反而要心算一遍时差。
        "session_start_iso": datetime.fromtimestamp(session_start).isoformat(),
        "count": len(clips),
        "params": {"top": top, "pad_before": pad_before,
                   "pad_after": pad_after, "min_score": min_score,
                   "types": sorted(tset) if tset else None, "lap": lap_no},
        # 权重透明化：消费方要知道排序口径，方便自己调
        "weights": dict(_HIGHLIGHT_WEIGHT),
        "clips": clips,
        "video": vinfo if vinfo else {"bound": False},
        "ffmpeg_hint": (
            f'ffmpeg -ss <clip_start_video> -i "{vinfo.get("file")}" '
            f"-t <duration> -c copy clip.mp4"
            if vinfo and vinfo.get("bound")
            else "ffmpeg -ss <clip_start> -i video.mp4 -t <duration> -c copy clip.mp4"),
    }


# ---------------------------------------------------------------------------
# 进站策略与名次（stint 分析 / 进站检测 / 实时名次时间线）
# ---------------------------------------------------------------------------

def _pit_compute(path: Path, grouped: dict, laps_list: list) -> dict[str, Any]:
    """session_pitstops 的慢路径（结果由调用方记忆化）。"""
    # 动力类型：gas_capacity == 0 → 纯电（与 recorder.classify_powertrain 同判据）。
    # 电车的 gas_level 是剩余电量 kWh，进站**不加油**，环跳判据不成立。
    # 取全场最常见容量（个别帧可能缺席）。
    caps = [f.get("gas_capacity") for fs in grouped.values() for f in fs]
    caps = [c for c in caps if c is not None]
    cap = max(set(caps), key=caps.count) if caps else None
    is_ev = (cap is not None and cap <= 1e-3)

    lap_nos = sorted(grouped.keys())
    pitstops: list[dict[str, Any]] = []
    stints: list[dict[str, Any]] = []

    # —— 进站检测：油量环跳（🔴 电车不检测：进站不加油，电量回升可能来自
    #    再生回充等与进站无关的语义，环跳判据不成立）——
    # 油量只会单调下降（消耗），唯一的大幅上升就是进站加油。
    # 本场实测：2.84 → 100.0 的一次环跳 = 进站加油，判据干净。
    PIT_JUMP = 5.0     # 比上一帧高出 5%（或 5 个单位）以上判为加油
    prev_gas = None
    prev_lap = None
    prev_t = None
    if not is_ev:
        for ln in lap_nos:
            for f in grouped[ln]:
                g = f.get("gas_level")
                t = f.get("t") or 0.0
                if g is not None and prev_gas is not None \
                        and g - prev_gas > PIT_JUMP:
                    pitstops.append({
                        "lap": int(ln),          # 出站圈（加油后第一帧所在圈）
                        "prev_lap": int(prev_lap) if prev_lap else int(ln),
                        "t_rel": round(t - (grouped[ln][0].get("t") or t), 3),
                        "before": round(prev_gas, 2),
                        "after": round(g, 2),
                    })
                if g is not None:
                    prev_gas, prev_lap, prev_t = g, ln, t

    # —— stint 切分：进站点把圈序列切段 ——
    # 每段：起止圈 / 帧跨度时长 / 段首段末油量 / 段内均耗。
    # （进站帧落在出站圈，所以该圈属于**新段**——段界 = 进站帧所在圈。）
    cut_laps = {p["lap"] for p in pitstops}
    segs: list[list[int]] = []
    cur: list[int] = []
    for ln in lap_nos:
        if ln in cut_laps and cur:
            segs.append(cur)
            cur = []
        cur.append(ln)
    if cur:
        segs.append(cur)
    for i, seg in enumerate(segs):
        fs = [f for ln in seg for f in grouped[ln]]
        if not fs:
            continue
        gs = [f.get("gas_level") for f in fs if f.get("gas_level") is not None]
        g_start, g_end = (gs[0], gs[-1]) if gs else (None, None)
        used = (g_start - g_end) if (g_start is not None and g_end is not None) \
            else None
        dur = (fs[-1].get("t") or 0.0) - (fs[0].get("t") or 0.0)
        stints.append({
            "stint": i + 1,
            "from_lap": int(seg[0]), "to_lap": int(seg[-1]),
            "laps": len(seg),
            "dur_s": round(dur, 1),
            "gas_start": g_start, "gas_end": g_end,
            "fuel_used": round(used, 2) if used is not None else None,
            "fuel_per_lap": (round(used / len(seg), 2)
                             if used is not None and seg else None),
            # 该段是不是从进站出来的（第一段 = 起步，不算进站后）
            "after_stop": i > 0,
        })

    # —— 实时名次：quali_pos(0x84) 在比赛中 = 当前名次（逐帧变化）——
    # 取每帧的有效名次（1..numCars；0/65535/None 是菜单态哨兵），相邻变化
    # 即超车 / 被超事件。t_rel 相对该圈起点，与 /series?lap=N、/events 同基准。
    positions: list[dict[str, Any]] = []
    pos_events: list[dict[str, Any]] = []
    prev_pos = None
    for ln in lap_nos:
        fs = grouped[ln]
        t0 = fs[0].get("t") or 0.0
        for f in fs:
            v = f.get("quali_pos")
            if not v or v >= 65000:
                continue
            if v != prev_pos:
                e = {"lap": int(ln), "pos": int(v),
                     "t_rel": round((f.get("t") or t0) - t0, 3)}
                if prev_pos is not None:
                    e["from"] = int(prev_pos)
                    # 正 = 被超（名次数字变大），负 = 超车
                    e["delta"] = int(v - prev_pos)
                pos_events.append(e)
                prev_pos = v
        if prev_pos is not None:
            positions.append({"lap": int(ln), "pos": int(prev_pos)})
    return {
        "available": True,
        "powertrain": "electric" if is_ev else "fuel",
        "pitstops": pitstops,
        "stints": stints,
        "positions": positions,
        "pos_events": pos_events,
        "laps_list": [int(x["lap"]) for x in laps_list],
        "lap_durs": {int(ln): round(grouped[ln][-1].get("t")
                                    - grouped[ln][0].get("t"), 3)
                     for ln in lap_nos if len(grouped[ln]) >= 2},
    }


def session_pitstops(path: Path) -> dict[str, Any]:
    """进站策略与名次：进站检测 / stint 分析 / 实时名次时间线。

    三块数据一次算完（全部来自广播已有的帧字段，不依赖协议没有的东西）：
      · 进站 = 油量环跳（gas_level 比上一帧高 >5）。GT7 油量只会单调消耗，
        唯一的大幅上升就是进站加油；电车（gas_capacity==0）不检测。
      · stint = 进站点切开的连续跑段，每段给起止圈 / 均耗。
      · 名次 = quali_pos(0x84) 在比赛中是**当前名次**（逐帧实时变，实测
        1~20 全出现、49 次变化）；0/65535 是菜单态哨兵，跳过。

    🔴 结果按场次记忆化（全场帧遍历一遍，冷 ~0.3s），命中 0ms。
    """
    try:
        laps_list, grouped, _ = _valid_laps(path)
    except Exception as e:
        return {"available": False, "reason": str(e)}
    if not grouped:
        return {"available": False, "reason": "no frames"}
    _, store = _load_frames(path)
    if not store:
        return {"available": False, "reason": "no frames"}
    return store.memo_compute(
        ("pitstops",), lambda: _pit_compute(path, grouped, laps_list))


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
            # 🔴 lap 必须走哨兵归一：菜单态 0xFFFF 会原样出现在历史窗里
            "brake": f.get("brake"), "lap": _lap_no(f),
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
                     # 三个都是 u16，菜单态会读成 0xFFFF → 必须归一，
                     # 否则第三方会拿到「发车位 65535」「65535 辆车」。
                     "grid_position": _u16(L.get("quali_pos")),
                     "grid_start": _u16(snap.get("grid_start")),
                     "num_cars": _u16(L.get("num_cars"))},
            "tyre_temp_c": L.get("tyre_temp", []),
            "suspension_height_m": L.get("susp_height", []),
            # 🔴 单位修正：这条通道是**角速度 rad/s**，不是「转/秒」。
            #    证据是自标定半径：R = Σ(v·ω)/Σ(ω²) 用自由滚动帧算出来是
            #    0.339 / 0.344 m（正常赛车轮胎量级）；若 ω 真是 rev/s，
            #    倒推半径会是 0.05 m —— 荒谬。旧键名字是错的。
            #    v1 对外「只加不改」：新增正确命名的键，旧键保留同一取值，
            #    免得打断已经在用 wheel_rev_per_s 的调用方（文档里标废弃）。
            "wheel_rad_per_s": L.get("wheel_rads", []),
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
            # 口径来源：lap = 真的本圈用时（接收器给了圈起点）；
            #           session = 兜底，退化成**场次**已用时（老接收器没写
            #           lap_started_at）。消费方据此决定信不信 ——
            #           给一个看起来正常的错数字比报错难查一百倍。
            "current_lap_time_source": snap.get("lap_time_source", "session"),
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
| POST | `/api/v1/settings/archive-after` | 设置自动归档保留天数，body `{"value": 30}`（0=不自动归档；归档为无损 `.jsonl.gz`） |
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
- `ref_lap=N`（sessions 详情 / 详情页）：指定参考圈号做行车轨迹/时间差对比分析，
  默认取最快圈；`cmp_lap=M`：指定被对比的圈，默认取最后一圈。
  圈号不存在或非有效（如手改 URL）时静默回退默认值。
- `cols=t,spd,rpm`（`/series` / `/frames` / `/csv`）：按需只取指定通道省流量；列名取自这些接口返回的 `cols` 字段。未知列名直接 400（附 `allowed` 列表），不静默少给几列；不传 = 全列（向后兼容）。
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
| `lap_started_at` | float | 当前圈起点的墙上时刻（秒，time.time()）。接收器每次冲线更新；仪表盘用它反推上一圈用时，作为 GT7 不报 last_lap 时的兜底圈速来源 |
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
| `wheel_rad_per_s` | rad/s | 四轮**角速度**，顺序 FL/FR/RL/RR；记录器存的是绝对值，恒非负 |
| `wheel_rev_per_s` | rad/s | ⚠️ **已废弃**：名字写「转/秒」而实际单位是 rad/s，为兼容旧调用方保留，取值与 `wheel_rad_per_s` 完全相同 |
| `flags` | bit 位 | bit0 在赛道 / bit1 暂停 / bit2 加载 / bit3 在挡 … |
| `state.on_track` | bool | 是否在赛道上（比赛进行中） |

### `timing`
| 字段 | 单位 | 说明 |
|---|---|---|
| `current_lap` | int | 当前圈号 |
| `laps_in_race` | int | 本局总圈数 |
| `best_lap_ms` / `last_lap_ms` | 毫秒 | 最快圈 / 上一圈（null = 还没跑完） |
| `current_lap_time_s` | 秒 | **本圈**已用时（从冲线起算，冲线归零） |
| `current_lap_time_source` | str | `lap` = 真本圈用时；`session` = 兜底口径（退化成场次已用时）。只在老接收器没写 `lap_started_at` 时出现 `session` |

### `track`
| 字段 | 说明 |
|---|---|
| `path` | `[[x, z, 该点G值, 油门, 刹车, 圈号, 速度], ...]` 约 10Hz 采样的整车轨迹 |
| `gg_samples` | `[[横向g, 纵向g], ...]` 约 16Hz 采样的 G-G 散点 |

> `path` 的点位格式在 v1 内**向后兼容地加长**过：早期版本只有 `[x, z, G]` 三个值，
> 现在补到 7 个（多出的油门/刹车/圈号/速度用于画「行车轨迹」）。
> 消费方请按长度判断，缺字段时把油门/刹车当 0 处理，不要假设一定有 7 个。

### `history[]`（每帧一条）
`t`（服务器时间戳秒）、`speed_kph`、`rpm`、`gear`、`throttle`、`brake`、
`lap`、`tyre_temp_c`、`g_force`。

> `lap` 的菜单态哨兵 `0xFFFF` 一律归一为 `0`（与 `series` / `frames` 同一口径），
> 所以历史窗里不会出现「第 65535 圈」。

## 历史场次的逐帧数据

场次 jsonl 里每帧有四十几个字段（速度/转速/档位/油门/刹车/G 力/四轮/油量…）。
下面三个接口把它按不同粒度暴露出来，供详情页与第三方分析使用。

| 接口 | 说明 |
|---|---|
| `GET /api/v1/sessions/<文件名>/series?lap=N&max_points=2400` | 整场（或第 N 圈）的**降采样**时序，用于画曲线 |
| `GET /api/v1/sessions/<文件名>/frames?offset=0&limit=200&lap=N` | **分页**逐帧数据（`limit` 上限 1000），用于表格 |
| `GET /api/v1/sessions/<文件名>/csv?lap=N` | 全量 CSV 下载（带 UTF-8 BOM，Excel 直接打开不乱码） |
| `GET /api/v1/sessions/<文件名>/raceline?lap=N` | 第 N 圈的**行车轨迹**（踏板 + G 力两套着色通道）；`lap` 缺省 = 最快圈 |
| `GET /api/v1/sessions/<文件名>/sectors?n=4` | **分段计时 + 理论最快圈**；`n` = 段数（2~10，缺省 4） |
| `GET /api/v1/sessions/<文件名>/slip?max_points=120` | **轮胎滑移**：空转 / 抱死检测；每圈曲线最多 `max_points` 点 |
| `GET /api/v1/sessions/<文件名>/deviation?ref_lap=&cmp_lap=&step=5` | **走线偏差**：本圈相对参考圈的逐米横向偏移热力图；`ref_lap` 缺省 = 最快圈，`cmp_lap` 缺省 = 最后一圈 |
| `GET /api/v1/sessions/<文件名>/events?lap=N` | **驾驶事件时间线**：打滑 / 碰撞 / 极限刹车 / 轮胎滥用 / 大油门 / 出界；`lap` 缺省 = 全部圈 |
| `GET /api/v1/sessions/<文件名>/highlights?top=10&pad_before=2&pad_after=1.5&min_score=0&types=&lap=` | **集锦剪辑时间轴**：事件按 `置信度×类型权重` 排序，每段给出可直接喂 ffmpeg 的 `clip_start` / `duration`（含前后留白）。`top=0` = 不限；`types` 逗号分隔过滤 |
| `GET /api/v1/sessions/<文件名>/video` | **录像↔遥测锚点**：读取当前绑定（文件 / `offset_s` / 换算好的起止时刻） |
| `POST /api/v1/sessions/<文件名>/video` | **绑定录像**：body 给 `{file, offset_s}` 或 `{file, video_lead_s}` 或 `{file, video_start_iso}` 或 `{file, video_start_epoch}` 或 `{file, probe:true}`；`{clear:true}` 解绑 |
| `GET /api/v1/sessions/<文件名>/pitstops` | **进站与名次**：进站检测（油量环跳）/ stint 分析 / 实时名次时间线 |
| `GET /api/v1/sessions/<文件名>/compare?ref_lap=N&cmp_lap=M` | **圈间对比数据**（时间差曲线 + 关键点配对）；详情页切参考圈/对比圈时只取这一份（轻量，且 `race_line=0` 可再省 73% 流量），不刷新整页 |
| `GET /api/v1/sessions/<文件名>/profile?lap=N&step=5&prominence=12` | 一圈的**按赛道位置索引**剖面：几何折线 + 等距遥测 + 刹车入点 / 弯心 / 给油点。`lap` 缺省 = 最快圈 |

`series` / `frames` 返回的 `cols` 固定为
`["t", "spd", "rpm", "thr", "brk", "gear", "glat", "glon", "fuel", "lap"]`：

| 列 | 含义 | 单位 |
|---|---|---|
| `t` | 相对时间（整场=从场次开始；选圈=从该圈起点） | 秒 |
| `spd` | 速度 | km/h |
| `rpm` | 发动机转速 | rpm |
| `thr` / `brk` | 油门 / 刹车开度 | %（0~100） |
| `gear` | 档位 | — |
| `glat` / `glon` | 横向 / 纵向 G（沿用游戏内 `g_force` 的横向在前定义） | g |
| `fuel` | 油量百分比；纯电车无此口径时为 `null` | % |
| `lap` | 圈号；`0` 表示尚未进入计时圈（菜单态哨兵 `0xFFFF` 也归一为 `0`） | — |

`series` 额外返回 `laps[]`（每圈 `lap` / `t0` / `dur` / `frames`）、
`total_frames`（整场帧数）、`scope_frames`（当前范围帧数）、
`sampled_frames`、`step`（抽稀步长）。

## 分段计时与轮胎滑移

这两张卡片回答的是「我还能快多少」和「我的胎是怎么被糟蹋的」。
两个接口都只依赖场次文件内容，服务端按场次记忆化，所以重复请求几乎零成本
（实测首次 0.39s / 0.53s，命中缓存后 4ms / 6ms）。

### `GET /api/v1/sessions/<文件名>/sectors?n=4` —— 分段计时 / 理论最快圈

每圈按**距离**等分成 `n` 段（不是按时间等分：只有把段边界钉在同一段路上，
跨圈的段用时才可比），插值出各段用时；每段取所有**可信圈**里的最快值求和，
就是「理论最快圈」。

| 字段 | 说明 |
|---|---|
| `n_sectors` / `tol` / `dist_tol` | 段数 / 圈速容差（0.05）/ 圈长容差（0.03） |
| `ref_dist_m` | 圈长中位数，圈长与它比对判断是否跑满整圈 |
| `actual_best_lap` / `actual_best_s` | 实际最快圈号 / 圈速 |
| `theoretical_best_s` | 理论最快圈（各段最快用时之和） |
| `potential_gain_s` / `potential_gain_pct` | 潜在空间 = 实际最快 − 理论最快 |
| `best_each_s[]` | 每段的场次最快用时 |
| `counted_laps[]` | 参与计算理论值的可信圈 |
| `partial_laps[]` | 圈长偏离中位数 >`dist_tol` 的圈，不给 `deltas` |
| `reliable` / `note` | 理论值是否可信；不可信时 `note` 写明原因 |
| `laps[]` | 逐圈 `{lap, total_s, dist_m, sectors[], counted, is_best, partial, deltas[]}` |

两条口径必须**先过滤再计算**，否则数字会骗人：

- **异常圈**：冲出赛道 / 进站 / 打转的圈里，某一小段可能"恰好很快"，把理论值
  拉到不真实地低。实测同一场 23 圈不过滤得到 `+8.48%` 的假空间，只留
  ≤最快圈×1.05 的 5 圈才是真实的 `+0.71%`。所以 `reliable` 只在可信圈
  ≥2 个时为 `true`，否则宁可空着也不给一个会骗人的数字。
- **残缺圈**：段边界按各圈**自己的**圈长等分，只有跑满整圈的圈边界才对得齐。
  实测第 1 圈只有 6129.6m（最快圈 6937.8m，录像从半圈处开始），它的第 1 段
  插出 25.2s，比全场最快的 33.2s 还"快" 8 秒 —— 纯属边界错位。更要紧的是
  圈长偏短的圈**总时长也偏短，完全可能被选成 `actual_best`**，所以这一步
  排在选最快圈**之前**，不只是从统计圈里剔掉。

> `theoretical_best_s < actual_best_s` 是**正常**的，不要当 bug「修」成相等。
> 残余偏差：同为完整圈时圈长仍有约 ±0.3% 的差（本次 5 个可信圈 6936~6960m），
> 段边界会错开十几米，理论值可能乐观 0.1~0.3s —— 所以 `dist_m` 要展示出来，
> 让人看得见可比性。

### `GET /api/v1/sessions/<文件名>/slip?max_points=120` —— 轮胎滑移

只有四轮角速度（`wheel_rads`）能反映「车轮实际转多快」，车身速度传感器
测不出空转和抱死。滑移率 `s = (ω·R − v) / v`：`s > 0` 轮子转得比车快
（空转），`s < 0` 转得比车慢（抱死）。

半径**自标定**，不依赖任何外部参数：自由滚动帧上 `ω·R ≈ v`，于是
`R = Σ(v·ω) / Σ(ω²)`（对 R 的最小二乘解）。**前后轴必须分别标定** ——
实测 R前 0.3391m / R后 0.3435m（比值 0.987）；若假设同半径，滑移率会被
整体偏置约 1.3%，而抱死的典型信号本身只有百分之几，偏置不可忽略。

| 字段 | 说明 |
|---|---|
| `available` | 缺 `wheel_rads` 的老场次为 `false`，前端据此整卡隐藏 |
| `calibration` | `{front_m, rear_m, ratio, free_frames, free_pct, ok, reason, free_slip_front, free_slip_rear}` |
| `laps[]` | 逐圈 `{lap, frames, front, rear, lockup_frames, wheelspin_frames}`；`front`/`rear` 为 `{min, p05, p50, p95, max}` |
| `lockup` / `wheelspin` | `{events, frames, worst[]}`；`worst[]` 最多 5 条 `{lap, t_rel, slip, speed_kph, throttle, brake, frames}` |
| `series` | 逐圈降采样曲线 `{t[], front[], rear[], speed_kph[], throttle[], brake[]}`，每圈 ≤`max_points` 点 |

- 事件按**连续帧**聚成一次（否则一次抱死会被算成 60 次），且至少 2 帧才算一次。
- 踏板要到位：抱死要求 `brake > 0.85`，空转要求 `throttle > 0.95`。
- `calibration.ok` 是自洽性检查：标定后自由滚动帧的滑移均值应接近 0
  （实测 −0.0001 / +0.0000）。偏得远说明标定被污染（自由滚动帧太少，
  或油门/刹车阈值没生效），此时 `reason` 写明原因。
- 自由滚动帧的噪声底 `|s| ≈ 0.0005`，而事件峰值从 `−1.000`（四轮全锁，
  ω 精确为 0）到 `+7.98`（低速全油门空转），阈值因此不敏感。
- ⚠️ 曲线是抽稀的，**1~2 帧的尖峰会漏掉**（抱死常常就这么短）——
  峰值一律从 `worst[]` 读，别从曲线读。

## 走线偏差

`GET /api/v1/sessions/<文件名>/deviation?ref_lap=&cmp_lap=&step=5`
—— 把「赛车线」「行进线」从「按 G + 踏板着色」升级成「按横向偏移量着色」。
本圈每一点投影到参考圈的折线上求垂足，dlat = 垂足到本圈点 在参考线法向上的投影；
前端按 dlat 标蓝 / 白 / 红，p95 截断色阶。回答的是「我在哪段路、开哪条线、偏了多远」。

| 字段 | 说明 |
|---|---|
| `ref_lap` / `cmp_lap` | 参考圈 / 对比圈；缺省 `ref_lap` = 最快圈、`cmp_lap` = 最后一圈 |
| `step` | 输出网格步长（米），1~50，缺省 5 |
| `ref_len_m` / `cur_len_m` | 参考圈 / 本圈的长度（米） |
| `coverage` | 本圈覆盖参考线的比例；< 0.6 ⇒ 残圈 ⇒ `reliable=false` |
| `reliable` / `reason` | 对齐是否可信；出场圈 / 残圈 / 距离对齐失败时不可比 |
| `rms_dlat` / `p95_abs_dlat` / `max_abs_dlat` | 偏移统计（米，p95 用于色阶截断） |
| `mean_dlat` | 整体偏离方向（带符号；坐标系手性决定正负，不要靠它判内侧外侧） |
| `inside_pct` | 弯心侧占比（按参考线自身曲率方向算）；直道 / 极贴线处为 `null` |
| `drift_m` | **诊断量**：本圈距离积分漂移（同一物理位置处本圈与参考线累计距离的差） |
| `start_arc_m` / `start_gap_m` / `end_gap_m` | 起点弧距、两圈起点物理间距、两圈终点物理间距；`start_gap_m > 60` ⇒ 出场圈 ⇒ 拒绝 |
| `ref_self_cross_m` | **诊断量**：参考线自交距离（沿赛道相隔 250m 的两点空间最近值）；仅展示，不作护栏 |
| `worst_win` | 10 段等弧长切分里 RMS 最大的那一段 `{from_m, to_m, rms_dlat, mean_dlat}` |
| `line[]` | 本圈走线重采样：`[x, z, dlat, inside]`；`inside ∈ {-1, 0, 1}` = {无效, 不在弯心侧, 在弯心侧} |
| `grid_m[]` | 与 `line[]` 一一对应的参考线弧长（米） |
| `ref_line[]` | 参考线等弧长采样 `[[x, z], ...]`（前端灰底图） |
| `segments[]` | 10 段等弧长切分：`{from_m, to_m, mean_dlat, max_abs_dlat, rms_dlat}` |
| `laps_list[]` | 本场所有有效圈号，给前端下拉用 |

- **几何对齐而不是距离对齐**：本圈每点投影到参考线折线上的最近线段。
  按距离对齐会被 `match_pv_pairs` 实测的 60~300 m 距离积分漂移污染
  （丢包时 `lap_samples` 跳过整段距离）；几何对齐天然免疫，
  沿赛道方向误差恒为 0，剩下的 dlat 才是真的横向差。
- **三道独立护栏，全过才给结论**：
  - `start_gap_m > 60` ⇒ 出场圈，跨圈回绕会让单调搜索锁错；
  - `coverage < 0.6` ⇒ 残圈（半圈起步 / 进站退出），不覆盖参考线；
  - `rms_dlat > 60` ⇒ 距离对齐明确失败，绝不吐出假横向偏移。
  三道都不过时返回 `reliable=false` + `reason`，但 `line` / `ref_line` 仍输出，
  前端照画灰底供肉眼参考，**RMS / 内侧占比这些数都别看**。
- **符号约定**：法向取参考线切向的 **+90° 旋转**（`N = (−Tz, Tx)`），
  所以「正 = 参考线行进方向的左侧」。但**别靠它判内侧外侧**——
  世界坐标的左右手性容易看反。要判内侧 / 外侧请用 `inside`
  （按参考线自身曲率方向算，不依赖坐标系手性）。

## 单圈行车轨迹

`GET /api/v1/sessions/<文件名>/raceline?lap=N` 只算**某一圈**的轨迹（缺省 `lap` = 最快圈，
该场没有有效圈时返回 `404` + `{"error":"no valid lap"}`）。返回：

| 字段 | 说明 |
|---|---|
| `lap` | 实际算的是第几圈 |
| `segments[]` | 按踏板通道切好的线段数组，每段 `{"color", "pts", "b", "t", "g"}` |

`pts` 是 `[[x, z], ...]`；`b` / `t` / `g` 与 `pts` **一一对齐**（长度相同），
分别是刹车开度 0~1、油门开度 0~1、合成 G 大小（`hypot(横向G, 纵向G)`）。
相邻两段首尾点重合（含各通道值），所以前端分多次 `stroke` 也不会有缺口。

> 分段是按**踏板**口径切的（`b`/`t` 判红/绿/滑行）。想按 G 力着色时，把同一条线的
> 所有点用 `g[]` 重上色即可——分段本身不影响 G 模式观感，只是多几次描边。

显式传入一个该场**不存在**的圈号不会报错，返回 `{"lap": N, "segments": []}`
（前端切圈时不必先探路）；只有「连最快圈都拿不到」才会 404。

## 圈间对比自选两圈

`GET /session?file=…&ref_lap=N&cmp_lap=M` —— `ref_lap` 是参考圈（缺省最快圈），
`cmp_lap` 是对比圈（缺省最后一圈）。两者都可手改，传入不存在或非有效的圈号会
**静默回退**到缺省值，不会把详情页打挂；两者撞成同一圈时自动换到另一圈。

⚠️ 时间差曲线是**按距离对齐**的，两圈圈长不同时曲线会截到短的那圈为止，因此
曲线末端的时间差**不等于**两圈圈速之差。详情页在两圈圈长相差 > 2% 时给出提示。

## 赛道自动识别

GT7 协议不下发赛道名（jsonl 表头 `circuit` 恒为 null），但轨迹形状稳定。
识别用**归一化形状指纹**：质心对齐 → RMS 半径归一 → 起点旋到 +X →
绕行方向统一为逆时针，再等弧长重采样 200 点。同一赛道两个场次的指纹
RMS 距离 ~0.006，最近的不同赛道 ~0.24（7 场实测，间隔 43 倍），
命中阈值取 `0.05`。

起点对齐**不需要旋转搜索**：同赛道车永远从同一物理位置过线。
反向跑的圈会在「镜像成逆时针」一步被拉齐——形状相同就命中同一条。

| 接口 | 说明 |
|---|---|
| `GET /api/v1/tracks` | 赛道库清单（id / name / ref_len_m / turns_cw / sessions 数） |
| `GET /api/v1/sessions/<文件名>/track` | 识别该场：返回 `{track_id, name, matched, distance, ref_len_m}`；首次会算指纹并落库 |
| `POST /api/v1/tracks/<id>/rename` `{"value": "名"}` | 赛道改名，同赛道所有场次一起生效 |

- 代表圈挑选：圈长（坐标折线长度）相对**中位数** ±5% 先剔脏圈/残圈，
  幸存圈里挑用时最短的——最快圈走线最干净。
- 库存 `data/tracks.json`（边车文件，jsonl 不可变）。`tracks[].name` 为空
  表示未命名，详情页显示「未命名赛道 #N」并给 ✎ 改名入口。
- 场次→赛道映射也在这个文件里：**详情页首次打开时识别并落库**，
  列表页只读映射展示赛道名，绝不触发识别本身（那要解析整场 jsonl）。
- 无有效圈的场次返回 `{error}`，不落库、不影响页面。

## 驾驶事件时间线

`GET /api/v1/sessions/<文件名>/events?lap=N` —— 从遥测里检测驾驶事件，
给复盘提供时间锚点。检测引擎是仓库里的 `gt7-event-detector.py`
（dashboard 用 importlib 就地加载；文件被裁剪掉时返回
`{"available": false, "reason": …}`，前端整卡隐藏，不报 500）。

| 字段 | 说明 |
|---|---|
| `available` | 检测器文件缺失 / 无帧时为 `false`，`reason` 写明原因 |
| `calibration` | 前后轴标定半径（与 `/slip` 共用同一次标定）；`available=false` 时滑移用兜底半径 |
| `events[]` | `{lap, t_rel, t_end_rel, type, confidence, evidence, hint, src}`，按 (圈, 圈内秒) 排序 |
| `type` | `spin`（打滑/失控）/ `collision`（碰撞）/ `hard_braking`（极限刹车）/ `tyre_abuse`（轮胎滥用）/ `heavy_throttle`（大油门出弯）/ `off_track`（出界） |
| `t_rel` / `t_end_rel` | 事件起止相对**该圈起点**的秒数（与 `/series?lap=N` 同一时间基准） |
| `src` | 事件来源：`telemetry`（检测器）/ `geometry`（走线偏差出界）/ `slip`（出界的滑移兜底） |
| `laps` / `lap_scope` | 本次覆盖的圈号清单 / `?lap=N` 的筛选值（0 = 全场） |
| `thresholds` | 本次检测用的全部阈值（透明化，别猜） |

- 圈口径与圈速表 / 赛车线**共用 `clean_laps`**——检测器自带的
  `find_lap_boundaries` 不剔假圈，菜单态 / 前圈不会混进来。
- 滑移率用前后轴分标定半径（`calibrate_wheel_radii`，与 `/slip` 同一次
  记忆化结果）；带符号定义与 `/slip` 一致：正 = 空转，负 = 抱死。
- **`off_track` 由走线偏差主导**：本圈相对参考圈 `|dlat| > 5m` 的持续段
  （最短 8m）判出界。压草地未必滑移大、高速冲出弯也出界——检测器自带的
  「低速 + 高滑移」判据只在偏差不可信时兜底（此时 `src=slip`）。
- 🔴 结果按场次记忆化：**冷路径 ~10s**（逐帧构造 21 万个 Sample 逐圈跑
  检测），所以事件卡**展开时才取**；同场次再取 0ms。别把它塞进详情页
  首屏串行链路。
- `tyre_abuse` 的 `evidence` 带 `四轮胎温_C`（1 位小数）与 `四轮悬挂_mm`
  （整数）——**都是真值**（2026-10-09 起胎温 / 悬挂进列式存储；实测两场真实
  jsonl 胎温 54~97℃、悬挂 0~0.3m）。
  🔴 胎温**必须保留小数**：四轮差异常常只有零点几度，取整会抹成
  `[60,60,60,60]`，而这个事件的判据恰恰就是「四轮不一样」。
- 🔴 `tyre_press` / `tyre_wear` 在格式 A 下**恒为 0**（GT7 不广播），
  因此既不存也不进证据——免得解说词里出现假数字。
- 🔴 **协议里没有方向盘角度**（2026-10-09 探针结论）。`spin` / `off_track`
  证据里的 `方向`（左 / 右）和 `过弯半径_m` 是用**曲率**换算的，不是协议字段：
  `κ = 横向G × 9.80665 / v²`，与轨迹几何曲率相关 0.986（误差中位 0.0004 1/m），
  符号用「外侧轮角速度更高」独立验证过（横向 G 为正 = 左转）。
  想复核 / 找真字段：`python tools/probe_fields.py --selftest`（自证工具）
  然后开车时 `--ps5 <IP> --seconds 25` 跑一次。

## 集锦高光（剪辑时间轴）

`GET /api/v1/sessions/<文件名>/highlights` —— 把上面的事件排成**可直接切片**
的集锦时间轴，是 POV 解说工具「自动剪集锦」的交接面。

| 字段 | 说明 |
|---|---|
| `session_start` / `session_start_iso` | 场次起点（墙上时钟 / 本地时间 ISO）——录像对齐的锚点 |
| `clips[]` | `{rank, score, type, type_cn, lap, confidence, t_start, t_end, clip_start, clip_end, duration, evidence, hint}` |
| `score` | `confidence × 类型权重`；权重随 `weights` 一起返回（碰撞 10 > 打滑 8 > 出界 7 > 轮胎滥用 5 > 大油门 4 > 极限刹车 3） |
| `t_start` / `t_end` | 事件本身，相对**场次起点**的秒数 |
| `clip_start` / `clip_end` / `duration` | 含前后留白的切片窗口：`ffmpeg -ss <clip_start> -i video.mp4 -t <duration>` |
| 参数 | `top`（0=不限，缺省 10）/ `pad_before`（2.0）/ `pad_after`（1.5）/ `min_score` / `types`（逗号分隔）/ `lap` |

- 🔴 切片窗口**必须带留白**：从事件正中间开始切，观众看不到"怎么发生的"。
- 事件只有 `t_rel`（相对圈起点），`/highlights` 负责把它还原成整场时间轴。

## 录像对齐（录像 ↔ 遥测 时间轴锚点）

录像和遥测是**两条独立的时间轴**，中间差一个只有玩家知道的常数（什么时候按的录制）。
不把这个常数存下来，`/highlights` 给的秒数就没法直接喂 ffmpeg —— 所以它是剪辑 / 配音的
前置条件。

🔴 **口径（三行一起看，弄反一个整段剪辑就错位）**

```
t_session = 相对「本场第一个有效圈起点」的秒数   ← /highlights 给的就是它
offset_s  = 录像开始时刻 − session_start          ← 负数 = 录像比遥测早开始
t_video   = t_session − offset_s                  ← ffmpeg -ss 要的是这个
```

例：开跑前 18 秒就按了录制 → `offset_s = −18` → 遥测第 30s 对应录像第 48s。
怕正负号搞反就用 `video_lead_s`（= −`offset_s`，"录像早开始 18 秒"填 18）。

| 字段 | 说明 |
|---|---|
| `bound` | 是否已绑定 |
| `file` | 录像文件路径（只是记下来，服务端不打开它） |
| `offset_s` / `video_lead_s` | 偏移 / 反向的直觉读法 |
| `session_start` / `session_start_iso` | 遥测时间轴零点（本地时区） |
| `video_start_epoch` / `video_start_iso` | 换算出的录像开始时刻 |
| `source` | `manual` / `ffprobe` / `mtime`——怎么定出来的 |

绑定后 `/highlights` 会多给一份录像时间轴：

- `video`：上面的绑定信息
- `clips[].clip_start_video` / `clip_end_video`：这段高光在**录像**里的秒数
- `ffmpeg_hint` 会换成带真实文件名的版本

存哪儿：`data/sessions_meta.json`（边车，与收藏 / 改名同一先例 —— jsonl 是不可变原始数据）。

🔴 **自动探测的坑**：`{"probe": true}` 只在录像文件**就在跑 dashboard 的这台机器上**时有效
（Docker 部署时看不到你电脑的文件）。有 `ffprobe` 会用 `mtime − duration` 反推开始时刻；
没有就只能用 mtime，而**多数录制软件（OBS / PS5 相册 / 采集卡）的 mtime 是"录完"的时刻**，
会偏晚整整一段录像的时长 —— 这种情况响应里会带 `warning`，别当准确值用。

## 进站与名次

`GET /api/v1/sessions/<文件名>/pitstops` —— 进站检测 / stint 分析 /
实时名次时间线，三块一次算完（全场帧遍历一遍 ~0.3s，结果记忆化）。

| 字段 | 说明 |
|---|---|
| `powertrain` | `fuel` / `electric`（按全场 `gas_capacity` 众数判定；电车 `gas_capacity==0`） |
| `pitstops[]` | `{lap(出站圈), prev_lap, t_rel(相对出站圈起点), before, after}`；判据 = 油量比上一帧高 >5 |
| `stints[]` | 进站切开的跑段：`{stint, from_lap, to_lap, laps, dur_s, gas_start, gas_end, fuel_used, fuel_per_lap, after_stop}` |
| `positions[]` | 每圈末名次 `{lap, pos}`（画时间线的骨架） |
| `pos_events[]` | 名次逐帧变化：`{lap, pos, t_rel, from, delta}`；`delta<0` 超车、`>0` 被超；首个事件无 `from` |
| `laps_list[]` / `lap_durs{}` | 有效圈号清单 / 每圈时长（秒） |

- 🔴 **进站判据是油量环跳**：GT7 油量只会单调消耗（进站加油才会大幅上升）。
  遍历范围是 `clean_laps` 的有效圈——菜单态/离场段的「油量重置回满」
  （场次结束后 gas 回 100）**不会**被误判成进站。电车**不检测**——进站不
  加油，电量回升可能来自再生回充，环跳判据不成立。
- 🔴 **名次来自 `quali_pos`(0x84)**：比赛进行中它是**当前名次**（逐帧实时变，
  实测一场 1~20 名全出现、49 次变化），不是排位成绩；0/65535 是菜单态哨兵。
  真正的发车位在接收器开跑瞬间的快照 `grid_start` 里。
- 🔴 **轮胎磨损广播协议里没有**（296 字节包无 wear 字段），这张卡**不含换胎
  判定**——轮胎寿命请看游戏内 HUD 自行判断。胎温（`tyre_temp`）协议里有，
  但列式存储未存、且温度 ≠ 磨损，不要拿它当磨损用。

## 圈剖面（给赛道工程师 / 第三方按位置索引一圈）

`GET /api/v1/sessions/<文件名>/profile?lap=N&step=5&prominence=12`

赛道工程师（gt7-coach）要的是「一圈的几何折线 + 等距遥测 + 刹车点/弯心/给油点」。
这些东西本服务里**都已经有了**（`clean_laps` / `lap_samples` / `find_peaks_valleys` /
距离重采样），消费方自己重写一遍不仅费力，还会因为实时侧只有 10Hz 轮询而精度更差、
第一圈完全没有参考。所以一次性打包发出去，谁都不用重复建图。

### 🔴 距离轴是几何弧长，不是速度积分

`/series`、`/sectors`、`/deviation` 用的是 `dist += v·Δt` 积分，**一圈漂移 60~300 m**。
拿它做「本圈 1200 m vs 参考圈 1200 m」对齐，实际赛道上能差十几米 —— 实时刹车点
预告会直接指错位置。`/profile` 改用 `car_x`/`car_z` 相邻弦长累积的**几何弧长**，
只依赖坐标，与速度无关，不漂移。两个口径都返回：

| 字段 | 口径 |
|---|---|
| `length_m` | 几何弧长（本接口的距离轴） |
| `length_by_speed_m` | 速度积分（老口径，留作对照） |
| `length_drift_pct` | 两者相差百分比；绝对值超过 3% 会在 `warnings` 里明说 |

### 返回结构

| 字段 | 说明 |
|---|---|
| `grid_m` | 等距网格（步长 `step`，米） |
| `speed_kph` / `throttle` / `brake` / `t_rel_s` / `glat` / `glon` | 与 `grid_m` **一一对齐**的通道 |
| `pt.x` / `pt.z` | 同一网格上的几何折线，消费方用它做实时最近点定位；本圈无坐标时为空数组 |
| `markers.brake_in[]` | 刹车入点：`s_in_m` / `s_out_m` / `speed_in_kph` / `peak_brake` / `duration_s` / `v_min_kph` |
| `markers.apex[]` | 弯心：`s_m` / `speed_kph` / `glat` / `radius_m` / `turn`（左/右）。半径由 `κ = G_lat·g/v²` 反推 —— 协议里**没有转向角** |
| `markers.throttle_on[]` | 出弯给油点：`s_m` / `speed_kph` / `after_apex_m` |
| `markers.peak[]` / `markers.valley[]` | 原始速度极值点（峰/谷交替） |
| `warnings[]` | 降级与口径提示。**空数组才是干净的**，非空必须往界面上显 |

`step` 缺省 5 m（clamp 到 1~100），`prominence` 缺省 12 km/h（clamp 到 1~60，
峰谷显著度阈值）。点太多（>3000）会自动放大步长，实际值回填在 `step_m`。

圈长不足 200 m、或帧数不足 20、或该圈不在有效圈里时返回 `{"error": ...}`
（额外给 `why` 与 `available_laps`），**不返回半成品关键点**。

### 🔴 正在跑的那一圈不能当参考圈

一场正在录制的比赛里，圈号最大的那一圈还在跑：它时长更短（在 `clean_laps`
的速度积分口径下反而"最快"）、坐标折线只覆盖半条赛道。拿它当参考，实时最近点
定位会大面积失配 —— 实测横向误差飙到 **300 m**（≈ 车在 5 秒里跑过的距离），
而表现形式是"偶尔算错"而不是报错。

所以录制进行中时，本接口会排除**原始帧里圈号最大的那一圈**（判据取原始帧的
最大圈号，不是 `clean_laps` 过滤后的 —— 半圈常已被当假圈剔掉，那时取过滤后的
最大值会误伤一个跑完了的合法圈）。`meta.recording` 会说明当前是否录制中，
`why` 为 `lap_in_progress` 时表示请求的正是那一圈，`no_lap` 表示一圈都还没跑完。

**⚠️ 有一个例外必须由调用方自己兜住：本场只有一圈时。** 那时上面那条排除
规则会把唯一的一圈也排掉（等于永远没有参考圈可用），所以接口会**退回把这一圈
（半圈）发出去**。而这一刻 `recording` / `available_laps` 与"已经跑完一圈"时
**长得一模一样** —— 光看"有没有拿到 profile"分不出来。所以响应里明说了：

| 字段 | 含义 |
| --- | --- |
| `meta.in_progress` | **返回给你的这一圈还在跑**（只有上面那个例外才为 `true`）。消费方必须据此拒绝它 |
| `meta.in_progress_lap` | 原始帧里圈号最大的那一圈（= 正在跑的那一圈；没在录制时为 `null`） |

`s_m` 那套最近点定位对这个字段特别敏感：半圈的折线只覆盖半条赛道、圈长还随车
前进一直变（实测一场里 5491 m 一路涨到 7493 m），拿它定位会得到几十米的横向
误差（表现为"出界了"和指错位置的刹车预告），**而且只在开局那几十秒出现**，
极难复现。

## 使用示例

```bash
# 实时数据（最近 60 帧）
curl "http://localhost:8787/api/v1/live?frames=60"

# 只要圈速
curl "http://localhost:8787/api/v1/laps"

# 历史场次列表，然后取某场的统计
curl "http://localhost:8787/api/v1/sessions"
curl "http://localhost:8787/api/v1/sessions/20261007_045628_unknown_6ac5607c.jsonl"

# 详情页分析（行车轨迹 / 时间差对比）；?ref_lap=N 参考圈（缺省=最快圈）、?cmp_lap=M 对比圈（缺省=最后一圈）
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl"
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl&ref_lap=3"
curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl&ref_lap=3&cmp_lap=7"

# 单圈行车轨迹（踏板 + G 力两套着色通道）
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/raceline?lap=3"

# 走线偏差（参考圈 vs 对比圈，逐米横向偏移）
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/deviation"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/deviation?ref_lap=6&cmp_lap=7&step=5"

# 赛道自动识别（首次识别并落库，之后走缓存）
curl "http://localhost:8787/api/v1/tracks"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/track"

# 逐帧数据：整场时序 / 第 3 圈时序 / 翻页 / 导出
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/series"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/series?lap=3"
curl "http://localhost:8787/api/v1/sessions/SESSION.jsonl/frames?offset=200&limit=200"
curl -o lap3.csv "http://localhost:8787/api/v1/sessions/SESSION.jsonl/csv?lap=3"
```

## 稳定性说明

- v1 字段**只加不改名不改单位**；将来不兼容的改动会升到 v2 并保留 v1
- `history` 的条数上限 600；`path`/`gg` 上限 4000/1200 点
- `frames` 的 `limit` 上限 1000；`series` 的 `max_points` 上限 6000
- 服务器单线程 HTTP，请勿高频轮询（≥100ms 间隔为宜）

## 传输与归档

### 响应压缩与连接复用
- **gzip 压缩**：客户端带 `Accept-Encoding: gzip` 时，正文（JSON / HTML / 页面）自动压缩（level 6）；附件类下载（CSV、`/download`）**不压缩**，避免浏览器把文件存成 `.gz`。可用 `--no-gzip` 启动参数关掉（排障 / 抓包看明文）。
- **HTTP/1.1 keep-alive**：默认连接复用；客户端可发 `Connection: close` 显式关闭。多请求场景（如详情页并行取数）较无 keep-alive 提速约 10×。

### 圈间对比轻量接口 `/compare`
`GET /api/v1/sessions/<文件名>/compare?ref_lap=N&cmp_lap=M`（可加 `&race_line=0`）返回 `compare_session` 同款结构，**不含**占 73% 体积的 `race_line`（行车轨迹卡会自己另行取 `/raceline`）。详情页切参考圈 / 对比圈时只取这一份并重画依赖它的三块，不刷新整页（整页重来约 196KB / 720ms，XHR 约 8KB / 590ms），且不闪屏。

### 存储归档（无损）
- 老场次自动 gzip 压缩为 `<名>.jsonl.gz`（体积约 1/8~1/10，**无损、字节可还原**），后台静默进行，不阻塞列表页；正在录制（文件近 20s 内有写入）的活场不归档。
- `GET /api/v1/sessions` 每条带 `archived: bool` 字段标明是否已压缩。
- 自动归档保留期：`POST /api/v1/settings/archive-after {"value": 30}`（天，0=不自动归档，上限 3650）。
- 访问归档场次无需改 URL：`/session`、`/api/v1/sessions/<名>`、`/series`、`/csv` 等都会用 `resolve_session` 自动兜底成 `.jsonl.gz`；`/download` 与 CSV 导出会自动带 `.gz` 后缀。
- 收藏 / 自定义名 / 赛道库映射都以**未压缩基名** `X.jsonl` 为键，归档只换后缀不丢标注。
"""


# —— 响应压缩 ——
# 60Hz 遥测的 JSON 冗余极大（重复的键名、成串的数字），series 147 KB / csv 10.6 MB
# 全是明文，实测压缩比 5~10 倍。代价是 CPU：level 6 压 1 MB 约 10ms 量级，
# 对一台还要收 60Hz UDP 的小机器来说，level 9 不值那点收益。
_GZIP_MIN_BYTES = 1024      # 小于 1 KB 压了也省不了多少，白花 CPU
_GZIP_LEVEL = 6
_GZIP_ENABLED = True        # --no-gzip 关掉（排障 / 抓包时看明文）


# 版本号唯一定义处。发版时只改这一行。
# 命名习惯跟随 git tag：这里是 `1.0.1`，对应发行标识 `v1.0.1`。
# 之所以单独设一个常量：此前 HTTP `Server:` 头里写死过 "GT7Dashboard/1.0"，
# 发版时容易漏改（coach 那边已经踩过一次同样的坑）。
APP_VERSION = "1.0.1"

DEFAULT_PORT = 8787


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "GT7Dashboard/" + APP_VERSION

    # 历史场次目录。由 main() 挂在「服务器实例」上，
    # ⚠️ 不是挂在类上——handler 里读的是 self.server.history_dir，
    #    设到 Handler 类上是无效的（踩过：设类上 → 属性不存在报错）。
    # 用类变量做默认值兜底，main() 会覆盖 server 实例上的值。
    history_dir: str = "./data"

    # —— 传输层 ——
    # HTTP/1.1 ⇒ keep-alive：实时页 10Hz 轮询不再每次重开 TCP 连接。
    # 🔴 代价是每条响应必须有**准确**的 Content-Length，否则客户端会一直
    #    等剩下的字节（表现为页面转圈不结束）——所以压缩与长度必须同一处算。
    protocol_version = "HTTP/1.1"
    # 空闲连接占着一条线程（ThreadingHTTPServer 每连接一线程）；
    # 浏览器最多开 6 条，没超时的话关掉的标签页会一直留着线程。
    # 300s 既够长（不会打断 10MB CSV 的慢速下发），又能收回死连接。
    timeout = 300

    # -- 工具 -------------------------------------------------------------

    def _accepts_gzip(self) -> bool:
        """客户端是否接受 gzip（`Accept-Encoding: gzip`，含 q=0 的拒绝）。

        只认 **gzip**（不认 deflate / br），因为我们只会发 gzip；
        把 q=0 当成"别压"，否则给明确拒绝的客户端压了等于发乱码。
        """
        if not _GZIP_ENABLED:
            return False
        enc = (self.headers.get("Accept-Encoding") or "").lower()
        for tok in enc.split(","):
            tok = tok.strip()
            if not tok.startswith("gzip"):
                continue
            q = 1.0
            if ";q=" in tok:
                try:
                    q = float(tok.split(";q=", 1)[1].strip())
                except ValueError:
                    q = 0.0
            return q > 0
        return False

    def _send_body(self, body: bytes, ctype: str, code: int = 200,
                   cors: bool = False,
                   extra: list[tuple[str, str]] | None = None,
                   attach: bool = False) -> None:
        """所有响应体的唯一出口：在这里一处决定 gzip 与 Content-Length。

        🔴 顺序不能反：先压、再按**压完**的长度写 Content-Length。
           写成压缩前的长度，HTTP/1.1 长连接下客户端会一直等剩下的字节。

        🔴 附件（Content-Disposition）不压：浏览器对下载的处理各不相同，
           有的存成 .gz 再让你自己解压，Excel 更是直接看不懂。
        """
        hdrs: list[tuple[str, str]] = []
        if (not attach and len(body) >= _GZIP_MIN_BYTES
                and self._accepts_gzip()):
            z = gzip.compress(body, compresslevel=_GZIP_LEVEL)
            # 压了反而更大（本来就随机的数）就别压
            if len(z) < len(body):
                body = z
                hdrs.append(("Content-Encoding", "gzip"))
                # 缓存（哪怕是我们自己发的 no-store，中间代理也可能看这个）
                hdrs.append(("Vary", "Accept-Encoding"))
        if extra:
            hdrs.extend(extra)
        # 客户端要求关闭（或本来就是 HTTP/1.0）时回一个 Connection: close，
        # 别让对端傻等——长连接下"什么时候结束"只能靠 Content-Length 或这一条。
        if self.close_connection:
            hdrs.append(("Connection", "close"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in hdrs:
            self.send_header(k, v)
        if cors:      # 公开 API v1 带跨域头，第三方网页可直接调用
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: Any, code: int = 200, cors: bool = False) -> None:
        self._send_body(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8", code, cors)

    def _send_text(self, text: str, code: int = 200, cors: bool = False) -> None:
        self._send_body(text.encode("utf-8"),
                        "text/markdown; charset=utf-8", code, cors)

    def do_OPTIONS(self) -> None:
        """CORS 预检。"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def _send_html(self, html: str, code: int = 200) -> None:
        self._send_body(html.encode("utf-8"),
                        "text/html; charset=utf-8", code)

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

        # —— 归档保留期：POST /api/v1/settings/archive-after {"value": 天数} ——
        if parsed.path == "/api/v1/settings/archive-after":
            hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                body = {}
            try:
                v = float(body.get("value"))
            except (TypeError, ValueError):
                self._send_json({"error": "value 必须是数字（天数，0=不自动归档）"},
                                400, cors=True)
                return
            if not (0 <= v <= 3650):
                self._send_json({"error": "天数需在 0~3650 之间"}, 400, cors=True)
                return
            s = load_settings(hist)
            s["archive_after_days"] = v
            save_settings(hist, s)
            self._send_json({"ok": True, "archive_after_days": v,
                             "hint": "已生效；归档是无损压缩（.jsonl → .jsonl.gz）"},
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
            target = resolve_session(target)   # 归档后被删进回收站的是 .gz
            if (not str(target).startswith(str(trash)) or not target.exists()
                    or not is_session_file(target)):
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
        # /api/v1/tracks/<id>/rename —— 赛道改名（同赛道所有场次一起生效）
        if (len(seg) == 6 and seg[1:4] == ["api", "v1", "tracks"]
                and seg[5] == "rename"):
            hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            except Exception:
                body = {}
            try:
                tid = int(seg[4])
            except ValueError:
                self._send_json({"error": "track id 非法"}, 400, cors=True)
                return
            new = str(body.get("value") or "").strip()[:60]
            if not new:
                self._send_json({"error": "名称不能为空"}, 400, cors=True)
                return
            lib = load_tracks(hist)
            tr = next((t for t in lib["tracks"] if t["id"] == tid), None)
            if not tr:
                self._send_json({"error": "track not found", "id": tid},
                                404, cors=True)
                return
            tr["name"] = new
            save_tracks(hist, lib)
            self._send_json({"ok": True, "id": tid, "name": new,
                             "hint": "已保存，同赛道所有场次一起生效"},
                            cors=True)
            return

        # —— 录像 ↔ 遥测 锚点：POST /api/v1/sessions/<name>/video ——
        # 绑定 / 改偏移 / 自动探测 / 解绑，全走这一个端点（POST + body），
        # 不额外开 DELETE —— 免得 CORS 预检还要放行新方法。
        vseg = parsed.path.split("/")
        if (len(vseg) == 6 and vseg[1:4] == ["api", "v1", "sessions"]
                and vseg[5] == "video"):
            return self._handle_video_bind(vseg[4])

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
        # 已被归档的场次磁盘上只有 X.jsonl.gz，补一次兜底
        target = resolve_session(target)
        if (not str(target).startswith(str(hist)) or not target.exists()
                or not is_session_file(target)):
            self._send_json({"error": "session not found", "file": name},
                            404, cors=True)
            return

        meta = load_session_meta(hist)
        # 🔴 键一律用**未压缩**的那个名字（X.jsonl）：归档只换后缀，收藏和
        #    自定义名不能因为归档就消失。老数据的键本来就是这个，兼容。
        canon = session_stem(name) + ".jsonl"
        entry = meta.setdefault(canon, {})

        if action == "rename":
            new = str(body.get("value") or "").strip()[:60]
            if not new:
                self._send_json({"error": "名称不能为空"}, 400, cors=True)
                return
            entry["custom_name"] = new
        elif action == "favorite":
            entry["favorite"] = bool(body.get("value"))
        elif action == "archive":
            # 手动归档：就地压成 .jsonl.gz（无损）。压一场 250MB 要几秒，
            # 所以这里不做后台线程——用户点了就是想要它现在发生。
            if target.name.endswith(".gz"):
                self._send_json({"ok": True, "action": action, "file": name,
                                 "already": True, "hint": "这一场已经归档过了"},
                                cors=True)
                return
            if _recording_active(hist) and \
                    (time.time() - target.stat().st_mtime) < 20.0:
                self._send_json({"error": "这一场正在录制，结束后再归档"},
                                400, cors=True)
                return
            ok = archive_session(target)
            self._send_json({"ok": ok, "action": action, "file": name,
                             "hint": "已压缩为 .jsonl.gz" if ok
                                     else "归档失败（见服务端日志）"}, cors=True)
            return
        elif action == "delete":
            trash = hist / "_trash"
            trash.mkdir(exist_ok=True)
            # 🔴 用 target.name 而不是 name：归档后的真名带 .gz
            target.rename(trash / target.name)      # 移入回收目录，可找回
            meta.pop(canon, None)
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

    def _handle_video_bind(self, name: str) -> None:
        """录像锚点的写入端。

        body 任选一种给法（都行，多了以 offset_s 为准）：
          {"file": "...", "offset_s": -18}         直接给偏移
          {"file": "...", "video_lead_s": 18}      录像比遥测早开始 18 秒
          {"file": "...", "video_start_iso": "2026-10-08T19:50:30"}
          {"file": "...", "video_start_epoch": 1791482000}
          {"file": "...", "probe": true}           让服务端 stat/ffprobe 这个路径
          {"clear": true}                          解绑
        """
        hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
        target = resolve_session((hist / Path(name).name).resolve())
        if (not str(target).startswith(str(hist)) or not target.exists()
                or not is_session_file(target)):
            self._send_json({"error": "session not found", "file": name},
                            404, cors=True)
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            body = {}

        canon = session_stem(name) + ".jsonl"
        meta = load_session_meta(hist)

        if body.get("clear"):
            ent = meta.get(canon)
            if ent:
                ent.pop("video", None)
                save_session_meta(hist, meta)
            self._send_json({"ok": True, "bound": False,
                             "hint": "已解绑录像"}, cors=True)
            return

        vfile = str(body.get("file") or "").strip()
        if not vfile:
            self._send_json({"error": "缺少 file（录像文件路径）"}, 400, cors=True)
            return

        start = session_start_epoch(target)
        if start is None:
            self._send_json({"error": "这一场没有有效圈，定不了时间轴零点"},
                            400, cors=True)
            return

        src = "manual"
        if "offset_s" in body:
            try:
                off = float(body["offset_s"])
            except (TypeError, ValueError):
                self._send_json({"error": "offset_s 必须是数字"}, 400, cors=True)
                return
        elif "video_lead_s" in body:
            try:
                off = -float(body["video_lead_s"])
            except (TypeError, ValueError):
                self._send_json({"error": "video_lead_s 必须是数字"}, 400, cors=True)
                return
        else:
            vs = None
            raw_iso = str(body.get("video_start_iso") or "").strip()
            if raw_iso:
                try:
                    vs = datetime.fromisoformat(raw_iso).timestamp()
                except ValueError:
                    self._send_json(
                        {"error": "video_start_iso 格式不对，例："
                                  "2026-10-08T19:50:30"}, 400, cors=True)
                    return
            elif "video_start_epoch" in body:
                try:
                    vs = float(body["video_start_epoch"])
                except (TypeError, ValueError):
                    self._send_json({"error": "video_start_epoch 必须是数字"},
                                    400, cors=True)
                    return
            elif body.get("probe"):
                pr = probe_video_start(vfile)
                if not pr.get("ok"):
                    self._send_json({"error": pr.get("reason"), "probed": False},
                                    400, cors=True)
                    return
                vs = float(pr["start_epoch"])
                src = pr.get("source") or "probe"
                if pr.get("warning"):
                    body["_warn"] = pr["warning"]
            if vs is None:
                self._send_json(
                    {"error": "定不了录像开始时刻：给 offset_s / video_lead_s / "
                              "video_start_iso / video_start_epoch 之一，"
                              "或用 {\"probe\": true} 让服务端探测（前提是"
                              "录像文件在跑 dashboard 的这台机器上）"},
                    400, cors=True)
                return
            off = vs - start

        entry = meta.setdefault(canon, {})
        entry["video"] = {
            "file": vfile,
            "offset_s": round(float(off), 3),
            "source": src,
            "bound_at": round(time.time(), 3),
        }
        save_session_meta(hist, meta)
        info = video_session_info(hist, target)
        if body.get("_warn"):
            info["warning"] = body["_warn"]
        info["ok"] = True
        info["hint"] = (f"已绑定：遥测 0s ↔ 录像 {(-off):.1f}s"
                        f"（t_video = t_session - offset_s）")
        self._send_json(info, cors=True)

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
                target = resolve_session(target)     # 归档后只有 X.jsonl.gz
                # 目录穿越防护
                if (not str(target).startswith(str(hist))
                        or not target.exists() or not is_session_file(target)):
                    self._send_html("<h1>文件不存在或非法路径</h1>", 404)
                    return
                # 参考圈选择器：?ref_lap=N 指定赛车线看第几圈，缺省=最快圈。
                # 🔴 「最快圈」必须用 analyze_session 的口径（真实帧跨度、
                #    剔除 <20s 的残圈），而不是 analyze_compare 内部拿重采样
                #    duration 再取 min——两者分母不同，会出现「表格标第 6 圈
                #    最快、选择器却默认第 8 圈」的对不上。这里显式传 best_lap。
                # ?cmp_lap=M 指定拿哪一圈跟参考圈比（缺省=最后一圈）。
                ref_raw = query.get("ref_lap", [""])[0]
                cmp_raw = query.get("cmp_lap", [""])[0]
                stats = analyze_session(target)
                try:
                    ref_lap_no = int(ref_raw) if ref_raw else None
                except ValueError:
                    ref_lap_no = None
                try:
                    cmp_lap_no = int(cmp_raw) if cmp_raw else None
                except ValueError:
                    cmp_lap_no = None
                if ref_lap_no is None:
                    ref_lap_no = (stats.get("best_lap") or {}).get("lap")
                self._send_html(build_session_page(
                    target, stats, ref_lap_no=ref_lap_no,
                    cmp_lap_no=cmp_lap_no))

            elif path == "/api/session":
                name = query.get("file", [""])[0]
                if not name:
                    self._send_json({"error": "缺少 file 参数"}, 400)
                    return
                # 防目录穿越
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                target = resolve_session(target)     # 归档后只有 X.jsonl.gz
                if (not str(target).startswith(str(hist))
                        or not target.exists() or not is_session_file(target)):
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

            elif path == "/api/v1/tracks":
                # 赛道库清单：识别出的所有赛道 + 各自挂了多少场次
                hist = Path(self.server.history_dir)  # type: ignore[attr-defined]
                lib = load_tracks(hist)
                cnt: dict[int, int] = {}
                for s in lib["sessions"].values():
                    tid = s.get("track_id")
                    if tid is not None:
                        cnt[tid] = cnt.get(tid, 0) + 1
                tracks = [{
                    "id": t["id"], "name": t.get("name") or "",
                    "ref_len_m": t.get("ref_len_m"),
                    "turns_cw": t.get("turns_cw"),
                    "created": t.get("created"),
                    "sessions": cnt.get(t["id"], 0),
                } for t in lib["tracks"]]
                self._send_json({"meta": {"api_version": 1},
                                 "match_tol": _TRACK_MATCH_TOL,
                                 "tracks": tracks}, cors=True)

            elif path.startswith("/api/v1/sessions/") and path.endswith("/download"):
                # /api/v1/sessions/<名>/download —— 流式下发原始 jsonl
                seg = path.split("/")
                name = Path(seg[4]).name if len(seg) == 6 else ""
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                # 已被归档的场次磁盘上只有 X.jsonl.gz，补一次兜底
                target = resolve_session(target)
                if (not str(target).startswith(str(hist)) or not target.exists()
                        or not is_session_file(target)):
                    self._send_json({"error": "session not found", "file": name},
                                    404, cors=True)
                    return
                self.send_response(200)
                # 🔴 归档后下载到的是 .jsonl.gz：文件名和 MIME 都得说实话，
                #    否则用户拿到一个叫 .jsonl 的 gzip 文件（打不开且不知道为什么）。
                gzed = target.name.endswith(".gz")
                self.send_header(
                    "Content-Type",
                    "application/gzip" if gzed else "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{target.name}"')
                # 🔴 HTTP/1.1 + keep-alive：原始 jsonl 有几百 MB，不能读进内存
                #    再压，所以这条**不 gzip**，但 Content-Length 必须是真实
                #    字节数（否则客户端不知道下载何时结束）。
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

            elif path.startswith("/api/v1/sessions/") and (
                    path.endswith("/series") or path.endswith("/frames")
                    or path.endswith("/csv") or path.endswith("/raceline")
                    or path.endswith("/sectors") or path.endswith("/slip")
                    or path.endswith("/deviation") or path.endswith("/track")
                    or path.endswith("/events") or path.endswith("/pitstops")
                    or path.endswith("/compare")
                    or path.endswith("/highlights") or path.endswith("/video")
                    or path.endswith("/profile")):
                # 逐帧遥测三兄弟：/series（降采样画图）/ frames（分页表）/ csv（导出）
                # 外加 /raceline：单圈赛车线（「行车轨迹」卡片独立切圈用，
                #   不必重算整页对比分析）。
                # 外加 /sectors（分段计时 + 理论最快圈）、/slip（轮胎滑移）、
                #   /deviation（走线偏差）：三张分析卡片各自展开时才取，
                #   同样不拖累整页渲染。
                # 外加 /track：赛道自动识别（首次会算指纹并落库）。
                # 外加 /events：驾驶事件时间线（展开时才取，冷路径 ~10s）。
                # 🔴 必须排在下面那条「通用 /api/v1/sessions/<名>」之前，
                #    否则会被当成场次名吞掉。
                seg = path.split("/")
                name = Path(seg[4]).name if len(seg) == 6 else ""
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                # 已被归档的场次磁盘上只有 X.jsonl.gz，补一次兜底
                target = resolve_session(target)
                if (not str(target).startswith(str(hist)) or not target.exists()
                        or not is_session_file(target)):
                    self._send_json({"error": "session not found", "file": name},
                                    404, cors=True)
                    return
                lap_raw = query.get("lap", [""])[0]
                try:
                    lap_no = int(lap_raw) if lap_raw else None
                except ValueError:
                    lap_no = None
                if lap_no is not None and lap_no <= 0:
                    lap_no = None
                # ?cols=t,spd,rpm —— 只挑需要的通道（/series /frames /csv 用）。
                # 写错列名直接 400，而不是静默少给几列。
                sel, cols_err = _parse_cols(query.get("cols", [""])[0])
                if cols_err:
                    self._send_json({"error": cols_err, "allowed": _SERIES_COLS},
                                    400, cors=True)
                    return
                if path.endswith("/raceline"):
                    if lap_no is None:
                        # 不给 lap 就直接取最快圈，省得前端先问一次圈速表
                        st = analyze_session(target)
                        lap_no = (st.get("best_lap") or {}).get("lap")
                    if lap_no is None:
                        self._send_json({"error": "no valid lap", "lap": None,
                                         "segments": []}, 404, cors=True)
                        return
                    self._send_json(race_line_session(target, lap_no), cors=True)
                elif path.endswith("/sectors"):
                    try:
                        ns = int(query.get("n", ["4"])[0])
                    except ValueError:
                        ns = 4
                    self._send_json(
                        session_sectors(target, n_sectors=ns), cors=True)
                elif path.endswith("/slip"):
                    try:
                        mp = int(query.get("max_points", ["120"])[0])
                    except ValueError:
                        mp = 120
                    self._send_json(
                        session_slip(target, max_per_lap=mp), cors=True)
                elif path.endswith("/deviation"):
                    try:
                        rl = int(query.get("ref_lap", ["0"])[0]) or None
                    except ValueError:
                        rl = None
                    try:
                        cl = int(query.get("cmp_lap", ["0"])[0]) or None
                    except ValueError:
                        cl = None
                    try:
                        st = float(query.get("step", ["5"])[0])
                    except ValueError:
                        st = 5.0
                    self._send_json(
                        session_deviation(target, ref_lap=rl, cmp_lap=cl,
                                          step=st), cors=True)
                elif path.endswith("/track"):
                    self._send_json(session_track(target), cors=True)
                elif path.endswith("/events"):
                    self._send_json(session_events(target, lap_no=lap_no),
                                    cors=True)
                elif path.endswith("/highlights"):
                    # 集锦剪辑时间轴：事件按 置信度×类型权重 排序 + 前后留白，
                    # 输出可直接喂 ffmpeg 的 clip_start / duration。
                    # 传 history_dir：绑了录像的场次多给一份录像时间轴。
                    def _f(key: str, default: float) -> float:
                        try:
                            return float(query.get(key, [default])[0])
                        except (ValueError, TypeError):
                            return default
                    try:
                        top_n = int(query.get("top", ["10"])[0])
                    except ValueError:
                        top_n = 10
                    types_raw = query.get("types", [""])[0]
                    self._send_json(
                        session_highlights(
                            target, top=max(0, top_n),
                            pad_before=_f("pad_before", 2.0),
                            pad_after=_f("pad_after", 1.5),
                            min_score=_f("min_score", 0.0),
                            types=[t for t in types_raw.split(",") if t.strip()]
                            or None,
                            lap_no=lap_no, history_dir=hist),
                        cors=True)
                elif path.endswith("/video"):
                    # 录像 ↔ 遥测 锚点：读当前绑定（含换算好的起止时刻）
                    self._send_json(video_session_info(hist, target), cors=True)
                elif path.endswith("/pitstops"):
                    self._send_json(session_pitstops(target), cors=True)
                elif path.endswith("/compare"):
                    # 圈间对比（时间差曲线 + 关键点配对）：详情页切参考圈/对比圈
                    # 时**只**取这一份，不刷新整页 —— 整页重来要把 200k 帧的
                    # 统计、圈速表、四张分析卡一起重算一遍。
                    try:
                        rl2 = int(query.get("ref_lap", ["0"])[0]) or None
                    except ValueError:
                        rl2 = None
                    try:
                        cl2 = int(query.get("cmp_lap", ["0"])[0]) or None
                    except ValueError:
                        cl2 = None
                    if rl2 is None:
                        rl2 = (analyze_session(target).get("best_lap")
                               or {}).get("lap")
                    out = compare_session(target, ref_lap_no=rl2,
                                          cmp_lap_no=cl2)
                    # race_line 占整份响应的 73%（实测 98.6 KB 里 72.2 KB），
                    # 而切圈后「行车轨迹」卡本来就要自己去 /raceline 取新的一圈
                    # ——带上它等于白传一份马上就被覆盖的数据。
                    if query.get("race_line", ["1"])[0] in ("0", "false", "no"):
                        out.pop("race_line", None)
                    self._send_json(out, cors=True)
                elif path.endswith("/csv"):
                    body = session_csv(target, lap_no=lap_no,
                                       sel=sel).encode("utf-8")
                    # 归档后真名带 .gz，但导出的 CSV 该用**场次基名**
                    fn = session_stem(name) + (f"_lap{lap_no}" if lap_no
                                               else "") + ".csv"
                    # Excel 打开 UTF-8 CSV 需要 BOM，否则中文表头会乱码。
                    # attach=True：附件不 gzip（浏览器/Excel 对压缩下载的处理
                    # 五花八门，有的直接存成 .gz）。
                    self._send_body(b"\xef\xbb\xbf" + body,
                                    "text/csv; charset=utf-8", 200, cors=True,
                                    extra=[("Content-Disposition",
                                            f'attachment; filename="{fn}"')],
                                    attach=True)
                elif path.endswith("/frames"):
                    try:
                        off = int(query.get("offset", ["0"])[0])
                    except ValueError:
                        off = 0
                    try:
                        lim = int(query.get("limit", ["200"])[0])
                    except ValueError:
                        lim = 200
                    self._send_json(
                        session_frames(target, offset=off, limit=lim,
                                       lap_no=lap_no, sel=sel), cors=True)
                elif path.endswith("/profile"):
                    # 圈剖面：几何折线 + 等距遥测 + 刹车点/弯心/给油点。
                    # 给外部消费者（gt7-coach）一次性拿走，不必自己建图。
                    try:
                        pm = float(query.get("step", ["5"])[0])
                    except ValueError:
                        pm = 5.0
                    try:
                        pk = float(query.get("prominence", ["12"])[0])
                    except ValueError:
                        pk = 12.0
                    self._send_json(
                        session_profile(target, lap_no=lap_no,
                                        step_m=max(1.0, min(pm, 100.0)),
                                        prominence_kph=max(1.0, min(pk, 60.0))),
                        cors=True)
                elif path.endswith("/series"):
                    try:
                        mx = int(query.get("max_points", ["2400"])[0])
                    except ValueError:
                        mx = 2400
                    self._send_json(
                        session_series(target, lap_no=lap_no,
                                       max_points=max(200, min(mx, 6000)),
                                       sel=sel),
                        cors=True)

            elif path.startswith("/api/v1/sessions/"):
                # /api/v1/sessions/<文件名> —— 防目录穿越：只取文件名部分
                name = Path(path.rsplit("/", 1)[-1]).name
                hist = Path(self.server.history_dir).resolve()  # type: ignore[attr-defined]
                target = (hist / name).resolve()
                # 已被归档的场次磁盘上只有 X.jsonl.gz，补一次兜底
                target = resolve_session(target)
                if (not str(target).startswith(str(hist)) or not target.exists()
                        or not is_session_file(target)):
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


# ---------------------------------------------------------------------------
# 图表点击放大（仪表盘页 / 历史详情页共用）
# ---------------------------------------------------------------------------
# 主仪表盘（HTML_PAGE，裸字符串）与历史页（_page_shell，f-string）是两套外壳，
# 放大组件抽成常量各自注入，避免同功能维护两份实现。
#
# 两条放大路径：
#   · kind='node'   —— SVG / DOM 图表：克隆一份交给浏览器按矢量拉伸，不失真
#   · kind='canvas' —— 位图：按放大后的尺寸**重画**。把原位图拉大只会糊，
#                      所以画图函数必须能往任意 canvas 上画（见 drawXxxInto）。
ZOOM_CSS = r"""
.zoomer { cursor:zoom-in; }
.zoomer:hover { filter:brightness(1.05); }
#zoomWrap { position:fixed; inset:0; z-index:120; display:none;
  background:rgba(0,0,0,.6); backdrop-filter:blur(3px);
  align-items:center; justify-content:center; padding:18px; }
#zoomWrap.open { display:flex; }
#zoomBox { background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:14px 16px; max-width:96vw; max-height:94vh;
  overflow:auto; box-shadow:0 24px 60px rgba(0,0,0,.4); }
#zoomBox h3 { font-size:13px; font-weight:600; color:var(--text);
  margin-bottom:10px; }
#zoomBox h3 a { color:var(--muted); text-decoration:none; font-size:12px;
  font-weight:400; }
#zoomBox h3 a:hover { color:var(--accent); }
#zoomBody { display:block; }
#zoomHint { font-size:11.5px; color:var(--muted); margin-top:8px;
  text-align:center; }
"""

ZOOM_JS = r"""
// ---------- 图表点击放大 ----------
// 每张图登记 {title, kind, ar, src, draw}。放大版与屏幕上那份**共用同一段
// 绘制代码**——两套实现迟早会走偏（改了主页忘了放大版）。
var ZOOMABLES = {};
function registerZoom(key, def) { ZOOMABLES[key] = def; }

function zoomFit(ar) {
  var vw = window.innerWidth, vh = window.innerHeight;
  var dw = Math.min(vw * 0.9, 1500), dh = dw / (ar || 1.6);
  var maxH = vh * 0.74;
  if (dh > maxH) { dh = maxH; dw = dh * (ar || 1.6); }
  return [Math.round(dw), Math.round(dh)];
}

function openZoom(key) {
  var def = ZOOMABLES[key];
  var body = document.getElementById('zoomBody');
  if (!def || !body) return;
  document.getElementById('zoomTitle').textContent = def.title || '图表';
  body.innerHTML = '';
  if (def.kind === 'node') {
    // SVG：克隆后拉满宽度，矢量缩放（含文字）不失真。
    var src = document.getElementById(def.src);
    if (!src) return;
    var node = src.cloneNode(true);
    node.removeAttribute('id');
    if (node.classList) node.classList.remove('zoomer');
    var w = Math.round(Math.min(window.innerWidth * 0.9, 1500));
    var svgs = node.tagName === 'svg' ? [node] : node.querySelectorAll('svg');
    for (var i = 0; i < svgs.length; i++) {
      svgs[i].setAttribute('width', w);
      svgs[i].style.width = '100%';
      svgs[i].style.height = 'auto';
      svgs[i].style.maxWidth = '100%';
    }
    node.style.width = w + 'px';
    node.style.maxWidth = '100%';
    node.style.height = 'auto';
    body.appendChild(node);
  } else if (typeof def.draw === 'function') {
    var d = zoomFit(def.ar), dpr = Math.min(2, window.devicePixelRatio || 1);
    var cv = document.createElement('canvas');
    cv.width = Math.round(d[0] * dpr);
    cv.height = Math.round(d[1] * dpr);
    cv.style.width = d[0] + 'px';
    cv.style.height = d[1] + 'px';
    cv.style.display = 'block';
    body.appendChild(cv);
    // 用放大后的真实像素尺寸重画：字号/线宽按同比例放大才不显小
    def.draw(cv, cv.width, cv.height);
  }
  document.getElementById('zoomWrap').classList.add('open');
  document.body.style.overflow = 'hidden';
}

function closeZoom() {
  var w = document.getElementById('zoomWrap');
  if (w) w.classList.remove('open');
  var b = document.getElementById('zoomBody');
  if (b) b.innerHTML = '';
  document.body.style.overflow = '';
}

// 图表本身不可交互，用光标 + 点击来提示「可放大」
function makeZoomable(el, key) {
  if (!el) return;
  el.classList.add('zoomer');
  el.title = '点击放大';
  el.addEventListener('click', function () { openZoom(key); });
}

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape' || e.keyCode === 27) closeZoom();
});
"""

ZOOM_HTML = r"""
<div id="zoomWrap" onclick="if(event.target===this)closeZoom()">
  <div id="zoomBox">
    <h3><span id="zoomTitle">图表</span>
      <a href="#" style="float:right" onclick="closeZoom();return false">✕ 关闭（Esc）</a></h3>
    <div id="zoomBody"></div>
    <div id="zoomHint">放大视图为只读快照 · 按 Esc 或点击空白处关闭</div>
  </div>
</div>
"""


def build_page() -> str:
    # 用占位符替换而不是 f-string：整页 CSS/JS 里花括号上千个，
    # 转义一遍只会让人再也不敢改样式。
    return HTML_PAGE.replace("/*THEME_CSS*/", THEME_CSS) \
                    .replace("/*THEME_BOOT_JS*/", THEME_BOOT_JS) \
                    .replace("/*ZOOM_CSS*/", ZOOM_CSS) \
                    .replace("/*ZOOM_JS*/", ZOOM_JS) \
                    .replace("/*ZOOM_HTML*/", ZOOM_HTML)


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
{ZOOM_CSS}
</style></head><body>
<script>{ZOOM_JS}</script>
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
// 与 showLoad 配对：页面内异步操作（如按圈单独取行车轨迹）用它收遮罩。
// 跳转式的加载不需要调它——页面一换遮罩自然没了。
function hideLoad() {{
  var ov = document.getElementById('loadOv');
  if (ov) ov.style.display = 'none';
}}
</script>
<div class="top">
  <a class="back" href="/">&larr; 返回仪表盘</a>
  <h1>{title}</h1>
</div>
{body}
{ZOOM_HTML}
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


# 用 r""" 包起来：块内 JS 正则带反斜杠（/\.?0+$/），非 raw 字符串会被 Python
# 当转义处理，抛 SyntaxWarning: invalid escape sequence '\.'。
_COMPARE_TMPL = r"""
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
.lapbar { display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  margin:2px 0 8px; font-size:12.5px; color:var(--muted); }
.lapbar label { display:inline-flex; align-items:center; gap:6px; }
.lapbar select { font-size:12.5px; padding:4px 8px; border:1px solid var(--line);
  border-radius:7px; background:var(--card); color:inherit; font-family:inherit; }
.lapbar .side { font-weight:600; color:var(--fg); }
</style>
<div class="card">
  <h2>圈间对比分析</h2>
  <div class="lapbar">
    <label>参考圈 <select id="refLapSel" onchange="cmpLapChange()"></select></label>
    <label>对比圈 <select id="cmpLapSel" onchange="cmpLapChange()"></select></label>
    <span id="cmpSide" class="side"></span>
    <span style="flex:1"></span>
    <span id="cmpMeta"></span>
  </div>
  <p style="font-size:12px;color:var(--muted);margin-bottom:6px">
    曲线 = <b id="cmpLapName">对比圈</b>相对<b id="refLapName">参考圈</b>的逐距离时间差：
    <b style="color:var(--bad)">正（上）= 丢时间</b>，
    <b style="color:var(--ok)">负（下）= 更快</b>。两圈都可自选。</p>
  <p style="font-size:11.5px;color:var(--muted);margin-bottom:4px">
    横轴 = 圈内行驶距离（<b>0 = 起点线</b>），刻度下方的灰色时间是参考圈跑到该位置的时刻；
    红点 = 丢时间最多处，绿点 = 领先最多处。</p>
  <p class="dim" id="cmpWarn" style="font-size:11.5px;margin:2px 0 4px;display:none"></p>
  <svg id="diffSvg" class="cmp-svg" viewBox="0 0 720 236"></svg>
</div>
<div class="card">
  <h2>行车轨迹（第 <span id="rlLap">-</span> 圈）
    <span style="float:right;display:flex;gap:8px;align-items:center;text-transform:none">
      <select id="rlLapSel" onchange="rlLapChange(this.value)"
        style="font-weight:400;font-size:12.5px;padding:4px 8px;
               border:1px solid var(--line);border-radius:7px;
               background:var(--card);color:inherit;font-family:inherit"></select>
      <select id="rlModeSel" onchange="rlModeChange(this.value)"
        style="font-weight:400;font-size:12.5px;padding:4px 8px;
               border:1px solid var(--line);border-radius:7px;
               background:var(--card);color:inherit;font-family:inherit">
        <option value="pedal">按踏板着色（赛车线）</option>
        <option value="g">按 G 力着色</option>
      </select>
    </span>
  </h2>
  <canvas id="raceLineCv" width="760" height="440"></canvas>
  <div class="legend" id="rlLegend" style="justify-content:center;margin-top:6px"></div>
  <p class="dim" id="rlHint" style="font-size:11.5px;margin-top:8px"></p>
</div>
<div class="card">
  <h2>哪里快 / 哪里慢 —— 关键点对比</h2>
  <div id="pvSummary" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px"></div>
  <table class="pv-table">
    <thead><tr><th>距离 (m)</th><th>关键点</th>
      <th style="text-align:right" id="pvHeadCur">对比圈</th>
      <th style="text-align:right" id="pvHeadRef">参考圈</th>
      <th style="text-align:right">差值</th></tr></thead>
    <tbody id="pvBody"></tbody>
  </table>
  <p class="dim" style="font-size:11.5px;margin-top:8px">
    关键点 = 直道尾速（峰）与弯心速度（谷），按距离配对。差值 = 对比圈 − 参考圈：
    <b style="color:var(--ok)">绿 +</b> 对比圈更快，
    <b style="color:var(--bad)">红 −</b> 更慢。</p>
</div>
<script>
// 🔴 var（不是 const）：切参考圈/对比圈时整份数据会被换掉重画，
//    所以这份引用必须是可变的。
var CMP = __DATA__;
(function () {
  if (!CMP || CMP.error || !CMP.laps_analyzed) {
    const m = document.getElementById('cmpMeta');
    if (m) m.textContent = '圈数据不足，无法对比（至少跑完一圈的 30 帧）';
    return;
  }
  // 🔴 「最快」标注必须按真实圈速算，不能贴在当前参考圈上——
  //    否则用户切到别的圈，选择器会把那一圈也叫「最快」（实测踩过）。
  //    口径与左侧圈速表一致：≥20s 才算有效圈。
  const SUMLAPS = CMP.lap_summary || [];
  const VALIDLAPS = SUMLAPS.filter(s => s.duration_s >= 20);
  const FASTEST = VALIDLAPS.length
    ? VALIDLAPS.reduce((a, s) => s.duration_s < a.duration_s ? s : a).lap : null;
  const lapDur = n => {
    const s = SUMLAPS.filter(x => x.lap === n)[0];
    return s ? s.duration_s : null;
  };
  const lapDist = n => {
    const s = SUMLAPS.filter(x => x.lap === n)[0];
    return s ? s.distance_m : null;
  };
  function lapOpts(sel, selVal, role) {
    if (!sel) return;
    sel.innerHTML = '';
    SUMLAPS.forEach(function (s) {
      const o = document.createElement('option');
      const tags = [];
      if (s.lap === FASTEST) tags.push('最快');
      if (s.lap === selVal && s.lap !== FASTEST) tags.push(role);
      o.value = s.lap;
      o.textContent = '第 ' + s.lap + ' 圈 · ' + s.duration_s.toFixed(1) + 's'
        + (tags.length ? '（' + tags.join('·') + '）' : '');
      if (s.lap === selVal) o.selected = true;
      sel.appendChild(o);
    });
  }

  function refreshCmpLabels() {
    const rl = CMP.ref_lap, cl = CMP.cur_lap;
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    set('refLapName', '第 ' + rl + ' 圈');
    set('cmpLapName', '第 ' + cl + ' 圈');
    set('pvHeadRef', '参考圈 第 ' + rl + ' 圈');
    set('pvHeadCur', '对比圈 第 ' + cl + ' 圈');
    const rd = lapDur(rl), cd = lapDur(cl);
    const side = document.getElementById('cmpSide');
    if (side && rd != null && cd != null) {
      const diff = cd - rd;
      side.innerHTML = '第 ' + cl + ' 圈比第 ' + rl + ' 圈 '
        + (diff <= 0 ? '<span style="color:var(--ok)">快 '
          : '<span style="color:var(--bad)">慢 ')
        + Math.abs(diff).toFixed(3) + 's</span>';
    }
    const meta = document.getElementById('cmpMeta');
    if (meta) {
      meta.textContent = '共分析 ' + CMP.laps_analyzed + ' 圈';
    }
    // 🔴 两圈圈长差别大时要明说。时间差曲线是按**距离**对齐的，
    //    只画到两圈中较短的那条；圈长差得多时，曲线末端的正负号完全
    //    可能与「总圈速差」相反——不提示的话用户会以为数据算错了。
    const warn = document.getElementById('cmpWarn');
    if (warn) {
      const rd2 = lapDist(rl), cd2 = lapDist(cl);
      if (rd2 && cd2 && Math.abs(rd2 - cd2) / Math.max(rd2, cd2) > 0.01) {
        warn.style.display = 'block';
        warn.innerHTML = '⚠️ 两圈<b>圈长不同</b>（第 ' + rl + ' 圈 '
          + Math.round(rd2) + 'm，第 ' + cl + ' 圈 ' + Math.round(cd2)
          + 'm）：曲线是按距离对齐的，只画到较短的那一圈，'
          + '所以曲线末端的高低可能与上面的<b>总圈速差</b>不一致。';
      } else {
        warn.style.display = 'none';
      }
    }
  }
  refreshCmpLabels();

  // 参考圈 / 对比圈选择器本身在这里填充；切换的动作（cmpLapChange）定义在
  // 本 IIFE 末尾——它要等 drawCmpDiff / drawCmpPv / rlLapChange 都就位。
  lapOpts(document.getElementById('refLapSel'), CMP.ref_lap, '参考圈');
  lapOpts(document.getElementById('cmpLapSel'), CMP.cur_lap, '对比圈');
  const refSelEl = document.getElementById('refLapSel');
  const cmpSelEl = document.getElementById('cmpLapSel');
  if (SUMLAPS.length < 2) {        // 只有一圈时没必要选
    if (refSelEl) refSelEl.style.display = 'none';
    if (cmpSelEl) cmpSelEl.style.display = 'none';
  }

  // —— 时间差曲线（带坐标参考：X=圈内距离+参考圈时刻，Y=毫秒） ——
  // 抽成函数：切换参考圈 / 对比圈时**只重画这一段**，不刷新整页。
  window.drawCmpDiff = function () {
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
  };
  drawCmpDiff();

  // —— 行车轨迹（两套着色口径）——
  // 「按踏板着色」：线色来自记录的油门 / 刹车开度百分比，刹车「粉→红」、
  //   油门「青→绿」、都不踩 = 滑行。后端 race_line 已按通道切段，并把每点的
  //   开度 b[] / t[] 与 pts[] 一一对齐；这里逐「子线段」取两端均值插值上色，
  //   于是得到连续渐变而不是三块死色。
  // 「按 G 力着色」：线色来自合成 G 大小（横向 + 纵向），与实时仪表盘
  //   「按 G 力着色」同一套配色 —— 直观看哪里最吃抓地力。
  //
  // 画成 drawRaceLineInto(cv, W, H) 是为了「点击放大」能按放大后的真实像素
  // 重画——把原来的位图拉大只会糊。
  const cv = document.getElementById('raceLineCv');
  // 取当前主题的实际颜色：canvas 不继承 CSS 变量，只能手动读一次
  const cvar = n => getComputedStyle(document.documentElement)
    .getPropertyValue(n).trim() || '#888';
  const clamp01 = v => v < 0 ? 0 : (v > 1 ? 1 : v);
  const mix = (c1, c2, k) => 'rgb(' + Math.round(c1[0] + (c2[0] - c1[0]) * k)
    + ',' + Math.round(c1[1] + (c2[1] - c1[1]) * k)
    + ',' + Math.round(c1[2] + (c2[2] - c1[2]) * k) + ')';
  const PINK = [255, 105, 180], RED = [255, 40, 40];    // 刹车：粉 → 红
  const CYAN = [0, 188, 212], GREEN = [0, 200, 83];     // 油门：青 → 绿
  const segColor = (ch, k) => {
    k = clamp01(k || 0);
    if (ch === 'brake') return mix(PINK, RED, k);
    if (ch === 'throttle') return mix(CYAN, GREEN, k);
    return cvar('--accent');
  };
  // 🔴 G 力配色与实时仪表盘页的 gColor() 必须逐字一致（蓝→绿→橙→红，0~3g），
  //    否则同一辆车在实时页和历史页会是两种配色。改了记得两边一起改
  //    （tests/test_color_sync.py 会拦）。
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

  // 状态：着色模式（记进 localStorage，属于显示偏好）、当前圈、当前段数据
  const RL_LAPS = (CMP.lap_summary || []).map(s => s.lap);
  let rlMode = 'pedal';
  try { if (localStorage.getItem('gt7_rl_mode') === 'g') rlMode = 'g'; } catch (e) {}
  let rlLap = CMP.ref_lap;
  let rlSegs = (CMP.race_line || {}).segments || [];

  const PEDAL_LEGEND = '<span><i style="width:30px;background:linear-gradient(90deg,#00bcd4,#00c853)"></i>'
    + '油门（青 → 绿，越深越浓）</span>'
    + '<span><i style="width:30px;background:linear-gradient(90deg,#ff69b4,#ff2828)"></i>'
    + '刹车（粉 → 红，越重越红）</span>'
    + '<span><i style="background:var(--accent)"></i>滑行</span>';
  const G_LEGEND = '<span><i style="width:64px;background:linear-gradient(90deg,'
    + 'rgb(13,110,253),rgb(25,135,84) 34%,rgb(253,126,20) 67%,rgb(220,53,69))"></i>'
    + '低 G → 高 G（0g → 3g+）</span>';
  const PEDAL_HINT = '线色来自该圈记录的<b>踏板开度百分比</b>：刹车踩得越重越偏红'
    + '（轻点刹车偏粉），油门踩得越深越偏绿（浅踩偏青）；两段踏板都不踩的'
    + '滑行段用强调色。';
  const G_HINT = '线色来自该圈记录的<b>合成 G 力大小</b>（横向 + 纵向）：'
    + '0g 偏蓝、约 1.5g 转绿、3g 以上转红。弯心横向 G 高、直道低，'
    + '一眼看出哪里最吃抓地力。与实时仪表盘的「按 G 力着色」是同一套配色。';

  function refreshRlUI() {
    const lg = document.getElementById('rlLegend');
    if (lg) lg.innerHTML = rlMode === 'g' ? G_LEGEND : PEDAL_LEGEND;
    const hn = document.getElementById('rlHint');
    if (hn) hn.innerHTML = (rlMode === 'g' ? G_HINT : PEDAL_HINT)
      + ' 上方可切<b>看第几圈的轨迹</b>（默认跟随参考圈）。';
    const lb = document.getElementById('rlLap');
    if (lb) lb.textContent = rlLap;
    const ms = document.getElementById('rlModeSel');
    if (ms) ms.value = rlMode;
  }

  window.rlModeChange = function (v) {
    rlMode = (v === 'g') ? 'g' : 'pedal';
    try { localStorage.setItem('gt7_rl_mode', rlMode); } catch (e) {}
    refreshRlUI();
    drawRaceLineInto(cv, cv.width, cv.height);
  };

  window.rlLapChange = function (v) {
    const n = parseInt(v, 10) || 0;
    if (!n || n === rlLap) return;
    rlLap = n;
    // 只取这一圈的赛车线，不刷新整页 —— 走 /raceline，
    // 免得为了换个圈的轨迹把整场 200k 帧的对比分析重算一遍。
    if (typeof showLoad === 'function') showLoad('正在读取第 ' + n + ' 圈的轨迹…');
    const url = '/api/v1/sessions/' + encodeURIComponent(CMP.file || '')
      + '/raceline?lap=' + n;
    fetch(url).then(r => r.json()).then(function (d) {
      rlSegs = (d && !d.error) ? (d.segments || []) : [];
      refreshRlUI();
      drawRaceLineInto(cv, cv.width, cv.height);
      if (typeof hideLoad === 'function') hideLoad();
    }).catch(function () {
      rlSegs = [];
      refreshRlUI();
      drawRaceLineInto(cv, cv.width, cv.height);
      if (typeof hideLoad === 'function') hideLoad();
    });
  };

  (function fillRlLapSel() {
    const sel = document.getElementById('rlLapSel');
    if (!sel) return;
    if (RL_LAPS.length < 2) { sel.style.display = 'none'; return; }
    sel.innerHTML = RL_LAPS.map(function (n) {
      return '<option value="' + n + '"' + (n === rlLap ? ' selected' : '')
        + '>第 ' + n + ' 圈</option>';
    }).join('');
  })();

  function drawRaceLineInto(canvas, W, H) {
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    const segs = rlSegs;
    if (!segs.length) {
      ctx.fillStyle = cvar('--muted');
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('这一圈没有可用的轨迹数据', W / 2, H / 2);
      return;
    }
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    segs.forEach(s => s.pts.forEach(p => {
      if (p[0] == null) return;
      if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0];
      if (p[1] < z0) z0 = p[1]; if (p[1] > z1) z1 = p[1];
    }));
    if (!(x1 > x0 && z1 > z0)) return;
    const PAD = W * 0.03 + 6;
    const sc = Math.min((W - 2 * PAD) / (x1 - x0), (H - 2 * PAD) / (z1 - z0));
    const ox = (W - (x1 - x0) * sc) / 2 - x0 * sc;
    const oy = (H - (z1 - z0) * sc) / 2 - z0 * sc;
    const px = v => ox + v * sc, py = v => H - (oy + v * sc);
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.lineWidth = Math.max(3.2, W / 240);
    segs.forEach(s => {
      const b = s.b || [], t = s.t || [], g = s.g || [];
      for (let i = 1; i < s.pts.length; i++) {
        const a = s.pts[i - 1], c = s.pts[i];
        if (a[0] == null || c[0] == null) continue;
        if (rlMode === 'g') {
          // G 模式与通道无关，整条线按合成 G 重上色
          ctx.strokeStyle = gColor(((g[i - 1] || 0) + (g[i] || 0)) / 2);
        } else {
          // 子线段强度 = 两端开度均值：刹车段读 b[]，油门段读 t[]
          let k = 0;
          if (s.color === 'brake') k = ((b[i - 1] || 0) + (b[i] || 0)) / 2;
          else if (s.color === 'throttle') k = ((t[i - 1] || 0) + (t[i] || 0)) / 2;
          ctx.strokeStyle = segColor(s.color, k);
        }
        ctx.beginPath();
        ctx.moveTo(px(a[0]), py(a[1]));
        ctx.lineTo(px(c[0]), py(c[1]));
        ctx.stroke();
      }
    });
  }

  refreshRlUI();
  drawRaceLineInto(cv, cv.width, cv.height);

  // 详情页的行车轨迹也能点击放大（与仪表盘用同一套放大组件）
  if (window.registerZoom) {
    registerZoom('raceLine', {
      title: '行车轨迹', kind: 'canvas', ar: 760 / 440,
      draw: function (c, W, H) { drawRaceLineInto(c, W, H); }
    });
    makeZoomable(cv, 'raceLine');
    var dsvg = document.getElementById('diffSvg');
    if (dsvg) {
      registerZoom('diffSvg', { title: '圈间时间差曲线', kind: 'node', src: 'diffSvg' });
      makeZoomable(dsvg, 'diffSvg');
    }
  }

  // —— 关键点对比表（服务端已按圈内相对位置配好对）——
  // 口径：差值 = 对比圈 − 参考圈。正 = 对比圈更快（绿），负 = 更慢（红）。
  // 同样抽成函数：切圈时随 CMP 一起重画。
  const kindTxt = k => k === 'peak' ? '直道尾速' : '弯心速度';
  const dTxt = d => (d > 0 ? '+' : '') + d.toFixed(1);
  const dColor = d => d > 0.05 ? 'var(--ok)' : (d < -0.05 ? 'var(--bad)' : 'var(--muted)');
  window.drawCmpPv = function () {
  const rows = CMP.pv_pairs || [];
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
  };
  drawCmpPv();

  // —— 切参考圈 / 对比圈：只取 compare 这一份，就地重画 ——
  // 🔴 以前这里是「带着两个参数重新导航」= 整页刷新：整页要把 200k 帧的
  //    统计、圈速表、四张分析卡全部重算，切一次圈等好几秒（实测热缓存
  //    720ms / 196 KB）。实际上变的只有「哪两圈」，所以只换 CMP 并重画
  //    依赖它的三块即可（8 KB / 590ms，且不闪屏）。
  //    ⚠️ 别改回整页刷新——tests/test_compare_xhr.py 会拦。
  window.cmpLapChange = function () {
    const rs = document.getElementById('refLapSel');
    const cs = document.getElementById('cmpLapSel');
    const rv = rs ? rs.value : '', cvv = cs ? cs.value : '';
    const u = new URL(location.href);
    if (rs) u.searchParams.set('ref_lap', rv);
    if (cs) u.searchParams.set('cmp_lap', cvv);
    if (typeof showLoad === 'function') {
      showLoad('正在按第 ' + rv + ' 圈 vs 第 ' + cvv + ' 圈重新分析…');
    }
    // race_line=0：那份数据占响应的 73%，而下面 rlLapChange 会自己取新的一圈
    fetch('/api/v1/sessions/' + encodeURIComponent(CMP.file || '')
          + '/compare?ref_lap=' + encodeURIComponent(rv)
          + '&cmp_lap=' + encodeURIComponent(cvv) + '&race_line=0')
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || d.error) {
          if (typeof hideLoad === 'function') hideLoad();
          var m = document.getElementById('cmpMeta');
          if (m) m.textContent = (d && d.error) || '读取失败';
          return;
        }
        // 新这份没带 race_line（省流量），但别把已有的那份弄丢 ——
        // 万一 /raceline 那次取失败，画面上还有上一圈的线可看。
        if (!d.race_line && CMP.race_line) d.race_line = CMP.race_line;
        CMP = d;
        // 地址栏跟着变：刷新 / 收藏 / 分享链接还能拿到当前这两圈
        try { history.replaceState(null, '', u.toString()); } catch (e) {}
        refreshCmpLabels();
        drawCmpDiff();
        drawCmpPv();
        // 行车轨迹「默认跟随参考圈」，参考圈变了它也跟着换
        if (window.rlLapChange) window.rlLapChange(rv);
        // 走线偏差卡的参考圈也跟着走（它自己那张卡是异步取的）
        if (window.dvSetRef) window.dvSetRef(parseInt(rv, 10) || 0);
        if (typeof hideLoad === 'function') hideLoad();
      }, function () {
        if (typeof hideLoad === 'function') hideLoad();
        var m2 = document.getElementById('cmpMeta');
        if (m2) m2.textContent = '读取失败';
      });
  };
})();
</script>
"""


# 场次详情页的两张「真的能帮到开车」的分析卡片。
#
# 与「遥测数据」一样不内嵌数据：/sectors 要 0.39s、/slip 要 0.53s（本机 217k 帧），
# 内嵌会把首屏拖垮。改成页面渲染完后异步各取一次，服务端按场次记忆化，
# 之后再切段数 / 切圈几乎零成本。
#
# 两张卡片的分工：
#   分段计时 —— 回答「我还能快多少」，结论是理论最快圈与潜在空间；
#   轮胎滑移 —— 回答「胎是怎么被糟蹋的」，结论是空转/抱死的时刻与车速。
#
# 🔴 用 r""" 而不是 """：内嵌 JS 里的正则反斜杠不会被 Python 转义吃掉
#    （_COMPARE_TMPL 就是这里踩过，留下一条 SyntaxWarning）。
_DEVIATION_TMPL = r"""
<style>
  .dv-note { font-size:12px; color:var(--muted); margin:6px 0 10px; line-height:1.65; }
  .dv-canvas-wrap { position:relative; border:1px solid var(--line);
    border-radius:8px; background:rgba(128,128,128,.04); padding:4px; }
  .dv-canvas { display:block; width:100%; height:auto; }
  .dv-legend { display:flex; gap:14px; font-size:11.5px; color:var(--muted);
    flex-wrap:wrap; align-items:center; margin-top:8px; }
  .dv-legend i { display:inline-block; height:8px; border-radius:2px;
    margin-right:5px; vertical-align:1px; }
  .dv-bar { flex:1 1 200px; height:8px; border-radius:4px;
    background:linear-gradient(to right, #0d6efd, #e7ecef 49%, #e7ecef 51%, #dc3545);
    position:relative; }
  .dv-bar b { position:absolute; top:-2px; width:2px; height:12px;
    background:var(--text); border-radius:1px; }
  .dv-partial { background:rgba(255,193,7,.08); border:1px solid rgba(255,193,7,.4);
    border-radius:7px; padding:8px 12px; margin-top:8px; font-size:12px;
    color:var(--text); }
  .dv-partial b { color:var(--warn); }
</style>

<div class="card" id="cardDeviation">
  <h2>走线偏差（参考圈 vs 对比圈）
    <span style="float:right;display:flex;gap:8px;align-items:center;text-transform:none">
      <span id="dvMeta" style="font-weight:400;color:var(--muted)"></span>
      <label style="font-weight:400;font-size:12px;color:var(--muted)">对比圈
        <select id="dvLapSel" onchange="dvPick(this.value)"
          style="font-weight:400;font-size:12px;padding:3px 7px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:inherit;font-family:inherit"></select>
      </label>
    </span>
  </h2>
  <p class="dv-note">
    <b>横向偏移 = 本圈点 − 参考线垂足</b> 在参考线法向上的投影（米，带符号）。
    算法把参考圈当成折线，本圈每点投影到最近线段，几何对齐天然免疫距离积分漂移
    （<code>match_pv_pairs</code> 实测丢包时同帧 60~300 m）。
    颜色：<b style="color:#0d6efd">蓝</b> = 偏参考线一侧、<b style="color:var(--text)">白</b> = 贴线、
    <b style="color:#dc3545">红</b> = 偏另一侧；色阶按 <b>p95</b> 截断，避免单点尖峰把整圈压成单色。
    出场 / 残圈 / 距离对齐失败时整卡降级为「不可比」，并写明原因。
  </p>
  <div class="dv-canvas-wrap"><canvas id="dvCanvas" class="dv-canvas"
       width="780" height="460"></canvas></div>
  <div class="dv-legend">
    <span><i style="background:#9aa0a6"></i>参考线（灰底图）</span>
    <span style="flex:1;min-width:240px"><span style="font-size:11px">−p95</span>
      <span class="dv-bar" id="dvBar"></span>
      <span style="font-size:11px">+p95</span></span>
    <span><b id="dvScaleTxt" style="color:var(--text)"></b></span>
  </div>
  <div id="dvStats" class="an-stats" style="margin-top:14px"></div>
  <div id="dvPartial"></div>
  <p class="dv-note" style="margin-top:8px">
    <b>圈长偏差 / 距离积分漂移</b> 只是诊断量 ——
    「距离积分漂移」说的是同一物理位置处两圈<b>各自</b>累计距离的差，
    它本身不影响结论（算法按几何对齐、不按距离对齐），但数值大说明这一圈
    中途有过空转 / 锁死 / 丢包，对走线理解要打个折。
  </p>
</div>

<script>
// ---------- 走线偏差 ----------
(function () {
  var FILE = '__FILE__';
  var API = '/api/v1/sessions/' + encodeURIComponent(FILE);
  var DV = null, dvLapNo = 0;

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function fsgn(v) {
    if (v == null) return '-';
    return (v > 0 ? '+' : v < 0 ? '-' : '') + Math.abs(v).toFixed(2);
  }
  function f2(v) { return v == null ? '-' : (+v).toFixed(2); }
  function pct(v) { return v == null ? '-' : (+v).toFixed(1) + '%'; }
  function stat(label, main, sub, color) {
    return '<div class="an-stat"><span>' + esc(label) + '</span>'
      + '<b style="color:' + (color || 'inherit') + '">' + esc(main) + '</b>'
      + (sub ? '<em>' + esc(sub) + '</em>' : '') + '</div>';
  }

  // 发散色阶：dlat ∈ [−s, +s] 映射到蓝 → 白 → 红；s 由 p95 决定
  // （用 p95 而不是 max，避免单点撞墙把整圈压成单色）。
  function dvColor(v, s) {
    if (s <= 0) return '#9aa0a6';
    var t = Math.max(-1, Math.min(1, v / s));
    // t ∈ [-1, 0] → 蓝(13,110,253) → 白(231,236,239)
    // t ∈ [ 0, 1] → 白(231,236,239) → 红(220,53,69)
    var r, g, b;
    if (t < 0) {
      var k = -t;
      r = Math.round(13  + (231 - 13)  * k);
      g = Math.round(110 + (236 - 110) * k);
      b = Math.round(253 + (239 - 253) * k);
    } else {
      r = Math.round(231 + (220 - 231) * t);
      g = Math.round(236 + (53  - 236) * t);
      b = Math.round(239 + (69  - 239) * t);
    }
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }

  function drawDeviation() {
    var cv = el('dvCanvas'), ctx = cv.getContext('2d');
    var W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    if (!DV || !DV.line || !DV.line.length) {
      ctx.fillStyle = '#9aa0a6';
      ctx.font = '13px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('走线数据不可用', W / 2, H / 2);
      return;
    }
    // 计算坐标范围：以参考线 + 本圈线的并集为准，颜色按 dlat 着色。
    var x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
    var line = DV.line, ref = DV.ref_line || [];
    for (var i = 0; i < ref.length; i++) {
      var p = ref[i];
      if (p[0] < x0) x0 = p[0]; if (p[0] > x1) x1 = p[0];
      if (p[1] < z0) z0 = p[1]; if (p[1] > z1) z1 = p[1];
    }
    for (var j = 0; j < line.length; j++) {
      var q = line[j];
      if (q[0] < x0) x0 = q[0]; if (q[0] > x1) x1 = q[0];
      if (q[1] < z0) z0 = q[1]; if (q[1] > z1) z1 = q[1];
    }
    if (!(x1 > x0 && z1 > z0)) return;
    var PAD = Math.min(W, H) * 0.04 + 6;
    var sc = Math.min((W - 2 * PAD) / (x1 - x0), (H - 2 * PAD) / (z1 - z0));
    var ox = (W - (x1 - x0) * sc) / 2 - x0 * sc;
    var oy = (H - (z1 - z0) * sc) / 2 - z0 * sc;
    var px = function (v) { return ox + v * sc; };
    var py = function (v) { return H - (oy + v * sc); };

    // 灰底图：参考线
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.strokeStyle = 'rgba(154,160,166,.85)';
    ctx.lineWidth = Math.max(2.4, W / 320);
    if (ref.length > 1) {
      ctx.beginPath();
      ctx.moveTo(px(ref[0][0]), py(ref[0][1]));
      for (var k = 1; k < ref.length; k++) ctx.lineTo(px(ref[k][0]), py(ref[k][1]));
      ctx.stroke();
    }
    // 彩色线：本圈走线，逐段按 dlat 上色
    var scale = Math.max(DV.p95_abs_dlat || 0, 0.5);  // 色阶至少 0.5m
    ctx.lineWidth = Math.max(3.2, W / 220);
    for (var i = 1; i < line.length; i++) {
      var a = line[i - 1], b = line[i];
      if (a[0] == null || b[0] == null) continue;
      // 两点 dlat 均值上色，避免锯齿
      var mid = ((a[2] || 0) + (b[2] || 0)) / 2;
      ctx.strokeStyle = dvColor(mid, scale);
      ctx.beginPath();
      ctx.moveTo(px(a[0]), py(a[1]));
      ctx.lineTo(px(b[0]), py(b[1]));
      ctx.stroke();
    }
    // 把 0 标在线上（白点的位置）
    var bar = el('dvBar');
    if (bar) {
      var tk = bar.querySelector('b');
      if (!tk) { tk = document.createElement('b'); bar.appendChild(tk); }
      // 0 永远在条中点；不需要根据 p95 调位置
      tk.style.left = 'calc(50% - 1px)';
    }
    var st = el('dvScaleTxt');
    if (st) st.textContent = '色阶 ±' + scale.toFixed(2) + 'm';
  }

  function dvStats(d) {
    var h = '';
    // 主要诊断：RMS / p95 / max / 内侧占比 / 距离积分漂移
    h += stat('RMS 偏移', f2(d.rms_dlat) + ' m',
              '越小越贴线；典型干净圈 1~3m', d.rms_dlat < 3 ? 'var(--ok)' : 'var(--warn)');
    h += stat('p95 / 最大', f2(d.p95_abs_dlat) + ' / ' + f2(d.max_abs_dlat) + ' m',
              '色阶按 p95 截断');
    h += stat('平均偏移', fsgn(d.mean_dlat) + ' m',
              '正/负号看坐标系手性');
    h += stat('弯心侧占比', pct(d.inside_pct),
              '靠弯心走 vs 走外线',
              d.inside_pct == null ? 'var(--muted)'
              : (d.inside_pct > 55 || d.inside_pct < 45 ? 'var(--text)' : 'var(--ok)'));
    h += stat('覆盖参考线', pct(d.coverage * 100),
              '出场圈 / 残圈会 < 60%',
              d.coverage >= 0.6 ? 'var(--ok)' : 'var(--warn)');
    h += stat('距离积分漂移', (d.drift_m != null ? d.drift_m.toFixed(1) : '-') + ' m',
              '诊断量；大 ⇒ 中途丢过帧');
    // 最差段（10 段等弧长切分里 RMS 最大那一段）
    if (d.worst_win) {
      h += stat('最差段', 'S' + Math.round(d.worst_win.from_m) + '–S'
                          + Math.round(d.worst_win.to_m) + 'm',
                'RMS ' + f2(d.worst_win.rms_dlat) + 'm · 均值 '
                + fsgn(d.worst_win.mean_dlat) + 'm',
                'var(--bad)');
    }
    return h;
  }

  function renderDv() {
    var card = el('cardDeviation'), box = el('dvStats'), meta = el('dvMeta'),
        par = el('dvPartial');
    if (!card || !box) return;
    var d = DV;
    if (!d) return;
    if (d.error) {
      meta.textContent = '';
      box.innerHTML = '<p class="dv-note">' + esc(d.error) + '</p>';
      par.innerHTML = '';
      var cv = el('dvCanvas');
      if (cv) { var ctx = cv.getContext('2d');
        ctx.clearRect(0, 0, cv.width, cv.height);
        ctx.fillStyle = '#9aa0a6'; ctx.font = '13px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(d.error || '没有可用的圈', cv.width / 2, cv.height / 2);
      }
      return;
    }
    meta.textContent = '参考第 ' + d.ref_lap + ' 圈 · 对比第 ' + d.cmp_lap + ' 圈'
      + ' · 步长 ' + d.step_m + 'm';
    box.innerHTML = dvStats(d);
    if (!d.reliable && d.reason) {
      par.innerHTML = '<div class="dv-partial"><b>不可比：</b>' + esc(d.reason)
        + '<br/>下方仍画图供参考，但 RMS / 内侧占比 这些数都没意义，请忽略。</div>';
    } else {
      par.innerHTML = '';
    }
    drawDeviation();
  }

  // 参考圈被别处（圈间对比卡的「参考圈」下拉）改了 → 用它重取。
  // 走线偏差默认以参考圈为基准，两张卡必须同一个值，否则用户会以为
  // 「偏差是按第 3 圈算的」而曲线其实是第 5 圈的。
  window.dvSetRef = function (n) {
    var v = parseInt(n, 10) || 0;
    if (!v || (DV && v === DV.ref_lap)) return;
    var meta = el('dvMeta');
    if (meta) meta.textContent = '计算中…';
    fetch(API + '/deviation?ref_lap=' + v + '&cmp_lap=' + dvLapNo)
      .then(function (r) { return r.json(); })
      .then(function (d) { DV = d; renderDv(); }, function () {
        if (meta) meta.textContent = '读取失败';
      });
  };

  // lap 选择：参考圈固定 = 最快圈（服务端定的），只让用户换对比圈。
  window.dvPick = function (v) {
    dvLapNo = parseInt(v, 10) || dvLapNo;
    var meta = el('dvMeta');
    if (meta) meta.textContent = '计算中…';
    var refL = DV && DV.ref_lap;
    fetch(API + '/deviation?ref_lap=' + refL + '&cmp_lap=' + dvLapNo)
      .then(function (r) { return r.json(); })
      .then(function (d) { DV = d; renderDv(); }, function () {
        if (meta) meta.textContent = '读取失败';
      });
  };

  function dvFetchFail() {
    var meta = el('dvMeta'), box = el('dvStats');
    if (meta) meta.textContent = '读取失败';
    if (box) box.innerHTML = '<p class="dv-note">走线偏差数据请求失败（见控制台）。</p>';
  }

  // 🔴 与 _ANALYSIS_TMPL 同源：取数失败与渲染失败分开处理，
  //    避免渲染异常被 catch 吞掉而把整卡静默隐藏。
  fetch(API + '/deviation').then(function (r) { return r.json(); })
    .then(function (d) {
      DV = d; renderDv();
      // 填充对比圈下拉：去掉参考圈本身；按圈号升序。
      var sel = el('dvLapSel');
      if (sel && d.ref_lap != null && d.laps_list) {
        sel.innerHTML = '';
        var opts = (d.laps_list || []).filter(function (x) {
          return x !== d.ref_lap;
        });
        dvLapNo = d.cmp_lap;
        for (var i = 0; i < opts.length; i++) {
          var o = document.createElement('option');
          o.value = opts[i]; o.textContent = '第 ' + opts[i] + ' 圈';
          if (opts[i] === dvLapNo) o.selected = true;
          sel.appendChild(o);
        }
      }
    }, dvFetchFail);
})();
</script>
"""


_ANALYSIS_TMPL = r"""
<style>
.an-stats { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
.an-stat { flex:1 1 150px; border:1px solid var(--line); border-radius:8px;
  padding:7px 11px; background:rgba(128,128,128,.05); }
.an-stat span { display:block; font-size:11px; color:var(--muted); }
.an-stat b { display:block; font-family:var(--mono); font-size:17px;
  font-weight:600; margin-top:2px; }
.an-stat em { display:block; font-style:normal; font-size:11px;
  color:var(--muted); margin-top:1px; }
.an-bars { margin:2px 0 12px; }
.an-bar-row { display:flex; align-items:center; gap:9px; margin-bottom:5px; }
.an-bar-lab { flex:0 0 190px; font-size:11.5px; color:var(--muted); }
.an-bar { flex:1; height:16px; background:rgba(128,128,128,.14);
  border-radius:4px; overflow:hidden; display:flex; }
.an-bar i { height:100%; display:block; }
.an-bar-sum { flex:0 0 82px; text-align:right; font-family:var(--mono);
  font-size:12px; }
.an-note { font-size:11.5px; color:var(--muted); margin:6px 0; line-height:1.65; }
.an-note b { color:var(--text); }
.an-wrap { max-height:430px; overflow:auto; border:1px solid var(--line);
  border-radius:8px; margin-top:8px; }
.an-table { width:100%; border-collapse:collapse; font-size:12px; }
.an-table th, .an-table td { padding:4px 8px; border-bottom:1px solid var(--line);
  white-space:nowrap; }
.an-table th { position:sticky; top:0; background:var(--card); color:var(--muted);
  font-weight:500; font-size:11px; z-index:1; }
.an-table td.num { font-family:var(--mono); }
.an-table tbody tr:hover td { background:rgba(128,128,128,.07); }
.an-table tr.an-uncounted td { opacity:.5; }
.an-table tr.an-partial td { opacity:.4; }
.an-table tr.an-hot td { background:rgba(220,53,69,.07); }
.an-d { font-size:10px; opacity:.9; }
.an-tag { font-size:9.5px; color:var(--warn); border:1px solid var(--warn);
  border-radius:3px; padding:0 3px; }
.an-legend { display:flex; gap:16px; font-size:12px; color:var(--muted);
  justify-content:center; flex-wrap:wrap; margin-top:4px; }
.an-legend span { display:inline-flex; align-items:center; }
.an-legend i { display:inline-block; width:16px; height:4px;
  border-radius:2px; margin-right:5px; }
.an-sw { display:inline-block; width:11px; height:11px; border-radius:2px;
  margin-right:4px; vertical-align:-1px; }
</style>

<div class="card" id="cardSectors">
  <h2>分段计时 · 理论最快圈
    <span style="float:right;display:flex;gap:10px;align-items:center;text-transform:none">
      <span id="secMeta" style="font-weight:400;color:var(--muted)"></span>
      <label style="font-weight:400;font-size:12px;color:var(--muted)">段数
        <select id="secN" onchange="secReload(this.value)"
          style="font-weight:400;font-size:12px;padding:3px 7px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:inherit;font-family:inherit">
          <option value="2">2</option>
          <option value="3">3</option>
          <option value="4" selected>4</option>
          <option value="6">6</option>
          <option value="8">8</option>
        </select>
      </label>
    </span>
  </h2>
  <div id="secBody"><p class="an-note">正在计算分段…</p></div>
</div>

<div class="card" id="cardSlip">
  <h2>轮胎滑移（空转 / 抱死）
    <span style="float:right;display:flex;gap:8px;align-items:center;text-transform:none">
      <span id="slipMeta" style="font-weight:400;color:var(--muted)"></span>
      <select id="slipLapSel" onchange="slipPick(this.value)"
        style="font-weight:400;font-size:12px;padding:3px 7px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:inherit;font-family:inherit"></select>
    </span>
  </h2>
  <div id="slipBody"><p class="an-note">正在计算滑移…</p></div>
</div>

<script>
// ---------- 场次详情页 · 分段计时 / 轮胎滑移 ----------
(function () {
  var FILE = '__FILE__';
  var API = '/api/v1/sessions/' + encodeURIComponent(FILE);
  var SEC = null, SLIP = null, secN = 4, slipLapNo = 0;

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function f3(v) { return v == null ? '-' : (+v).toFixed(3); }
  function fsgn(v) {
    if (v == null) return '-';
    return (v > 0 ? '+' : v < 0 ? '-' : '') + Math.abs(v).toFixed(3);
  }
  function pct(v) { return v == null ? '-' : (+v * 100).toFixed(1) + '%'; }
  // 圈速差值：负 = 更快（绿），正 = 更慢（红）——与「圈间对比」卡片同一套口径
  function dcol(v) { return v < 0 ? 'var(--ok)' : v > 0 ? 'var(--bad)' : 'var(--muted)'; }
  function stat(label, main, sub, color) {
    return '<div class="an-stat"><span>' + esc(label) + '</span>'
      + '<b style="color:' + (color || 'inherit') + '">' + esc(main) + '</b>'
      + (sub ? '<em>' + esc(sub) + '</em>' : '') + '</div>';
  }
  var SCOLS = ['#0d6efd', '#7048e8', '#0c8599', '#f08c00', '#c2255c',
               '#198754', '#e8590c', '#5f3dc4', '#0b7285', '#a61e4d'];

  // ------------------------------------------------------------ 分段计时
  function secFind(d, lap) {
    for (var i = 0; i < d.laps.length; i++) if (d.laps[i].lap === lap) return d.laps[i];
    return null;
  }

  function secBars(d) {
    var be = d.best_each_s || [];
    var best = secFind(d, d.actual_best_lap);
    if (!best || !be.length || !d.reliable) return '';
    // 两条堆叠条共用**同一绝对刻度**（刻度 = 实际最快圈总时长）。
    // 若各自撑满 100%，两条就一样长，潜在空间正好看不出来——这才是这张图的重点。
    var scale = d.actual_best_s;
    var rows = [['实际最快圈（第 ' + best.lap + ' 圈）', best.sectors, best.total_s],
                ['理论最快圈（各段最快拼接）', be, d.theoretical_best_s]];
    var h = '<div class="an-bars">';
    for (var r = 0; r < rows.length; r++) {
      h += '<div class="an-bar-row"><span class="an-bar-lab">' + esc(rows[r][0])
        + '</span><div class="an-bar">';
      for (var k = 0; k < rows[r][1].length; k++) {
        h += '<i style="width:' + (rows[r][1][k] / scale * 100).toFixed(3)
          + '%;background:' + SCOLS[k % SCOLS.length] + '" title="S' + (k + 1)
          + ' ' + f3(rows[r][1][k]) + 's"></i>';
      }
      h += '</div><b class="an-bar-sum">' + f3(rows[r][2]) + 's</b></div>';
    }
    h += '<div class="an-bar-row"><span class="an-bar-lab" style="color:var(--ok)">'
      + '潜在空间</span><div class="an-bar"><i style="width:'
      + (d.potential_gain_s / scale * 100).toFixed(3) + '%;background:var(--ok)"></i>'
      + '</div><b class="an-bar-sum" style="color:var(--ok)">'
      + fsgn(d.potential_gain_s) + 's</b></div>';
    return h + '</div>';
  }

  function secTable(d) {
    var n = d.n_sectors, be = d.best_each_s || [];
    var head = '<tr><th>圈</th><th style="text-align:right">用时</th>'
      + '<th style="text-align:right">圈长</th>';
    for (var k = 0; k < n; k++) {
      head += '<th style="text-align:right">S' + (k + 1) + '</th>';
    }
    var body = '';
    for (var i = 0; i < d.laps.length; i++) {
      var r = d.laps[i];
      var cls = r.partial ? ' class="an-partial"'
        : (r.counted ? '' : ' class="an-uncounted"');
      body += '<tr' + cls + '><td>' + r.lap + (r.is_best ? ' ★' : '')
        + (r.partial ? ' <span class="an-tag" title="没跑满整圈：段边界不在同一段路上">残缺</span>' : '')
        + '</td><td class="num">' + f3(r.total_s) + '</td>'
        + '<td class="num" style="color:var(--muted)">' + Math.round(r.dist_m) + '</td>';
      for (var j = 0; j < n; j++) {
        var dl = r.deltas ? r.deltas[j] : null;
        var isBest = dl != null && Math.abs(dl) < 5e-4;
        var col = SCOLS[j % SCOLS.length];
        body += '<td class="num" style="text-align:right">'
          + '<div style="' + (isBest ? 'color:var(--ok);font-weight:600'
                                     : 'color:' + col + ';opacity:.92') + '">'
          + f3(r.sectors[j]) + '</div>'
          + (dl == null ? '' : '<div class="an-d" style="color:'
              + (isBest ? 'var(--ok)' : dcol(dl)) + '">'
              + (isBest ? '最快' : fsgn(dl)) + '</div>')
          + '</td>';
      }
      body += '</tr>';
    }
    return '<div class="an-wrap"><table class="an-table"><thead>' + head
      + '</thead><tbody>' + body + '</tbody></table></div>';
  }

  function renderSec(d) {
    SEC = d;
    var box = el('secBody'), meta = el('secMeta');
    if (!box) return;
    if (d.error || !d.laps || !d.laps.length) {
      if (meta) meta.textContent = '';
      box.innerHTML = '<p class="an-note">' + esc(d.error || d.note
        || '没有可用于分段的圈（采样点不足）') + '</p>';
      return;
    }
    if (meta) meta.textContent = '圈长基准 ' + Math.round(d.ref_dist_m) + 'm';
    var best = secFind(d, d.actual_best_lap);
    var h = '<div class="an-stats">';
    // 主数值一律是**秒数**，圈号放副位：三格一起看时读数位置才对得齐
    h += stat('实际最快圈', f3(d.actual_best_s) + 's',
              best ? '第 ' + best.lap + ' 圈' : '');
    if (d.reliable) {
      h += stat('理论最快圈', f3(d.theoretical_best_s) + 's',
                '由 ' + d.counted_laps.length + ' 个可信圈拼接', 'var(--ok)');
      h += stat('潜在空间', fsgn(d.potential_gain_s) + 's',
                fsgn(d.potential_gain_pct) + '%', 'var(--ok)');
    } else {
      h += stat('理论最快圈', '数据不足', d.note || '', 'var(--warn)');
    }
    h += '</div>' + secBars(d);
    h += '<p class="an-note">段边界按<b>距离</b>等分（不是按时间）：只有把边界钉在'
      + '同一段路上，跨圈的段用时才可比。理论最快圈 = 各段可信圈里最快用时之和，'
      + '它<b>小于</b>实际最快圈是正常的——差的这一段就是「你已经能跑出来、'
      + '只是还没在同一圈里连起来」的时间。</p>';
    if (!d.reliable && d.note) h += '<p class="an-note">⚠ ' + esc(d.note) + '</p>';
    if (d.partial_laps && d.partial_laps.length) {
      h += '<p class="an-note">第 ' + d.partial_laps.join('、') + ' 圈的圈长偏离基准 >'
        + Math.round(d.dist_tol * 100) + '%（没跑满整圈，或中途丢过帧）：段边界'
        + '不在同一段路上，因此<b>不参与</b>理论值、也不给差值。</p>';
    }
    h += secTable(d);
    box.innerHTML = h;
  }

  window.secReload = function (n) {
    secN = parseInt(n, 10) || 4;
    var meta = el('secMeta');
    if (meta) meta.textContent = '计算中…';
    fetch(API + '/sectors?n=' + secN).then(function (r) { return r.json(); })
      .then(renderSec).catch(function () {
        if (meta) meta.textContent = '读取失败';
      });
  };

  // ------------------------------------------------------------ 轮胎滑移
  function slipLine(s, vals, color, w, X, Y) {
    var d = '';
    for (var i = 0; i < s.t.length; i++) {
      // 滑移率截断到 ±100%：空转峰值能到 +798%，不截会把有用的 ±20% 压成一条平线。
      // 峰值一律以事件表为准，曲线只负责趋势。
      var q = Math.max(-1, Math.min(1, vals[i]));
      d += (i ? 'L' : 'M') + X(s.t[i]).toFixed(1) + ' ' + Y(q).toFixed(1) + ' ';
    }
    return '<path d="' + d + '" fill="none" stroke="' + color
      + '" stroke-width="' + w + '" stroke-linejoin="round"/>';
  }

  function slipChart(lap) {
    var s = (SLIP.series || {})[lap];
    if (!s || !s.t || s.t.length < 2) {
      return '<p class="an-note">该圈没有可画的曲线。</p>';
    }
    var W = 760, H = 250, L = 52, R = W - 12, T = 16, B = H - 30;
    var t0 = s.t[0], t1 = s.t[s.t.length - 1];
    var x = function (t) { return t1 > t0 ? L + (R - L) * (t - t0) / (t1 - t0) : L; };
    // 🔴 方向不能反：+滑移（空转）必须在**上**、−滑移（抱死）在**下**，
    //    与阈值线（上方 +10% 空转、下方 −15% 抱死）才自洽。
    var y = function (q) { return T + (B - T) * (1 - q) / 2; };   // q 归一化到 [-1,1]
    var h = '<svg class="cmp-svg" viewBox="0 0 ' + W + ' ' + H + '">';
    var lv = [-100, -50, 0, 50, 100];
    for (var i = 0; i < lv.length; i++) {
      var yy = y(lv[i] / 100).toFixed(1);
      h += '<line x1="' + L + '" y1="' + yy + '" x2="' + R + '" y2="' + yy
        + '" stroke="rgba(128,128,128,' + (lv[i] === 0 ? '.5' : '.2') + ')"'
        + (lv[i] === 0 ? ' stroke-dasharray="4 4"' : '') + '/>';
      h += '<text x="' + (L - 6) + '" y="' + (+yy + 3.5)
        + '" text-anchor="end" font-size="10" fill="var(--muted)">'
        + lv[i] + '%</text>';
    }
    // 事件阈值线：曲线越过它就是判为一次空转/抱死的地方
    var thr = [[-0.15, '#dc3545'], [0.10, '#f08c00']];
    for (var j = 0; j < thr.length; j++) {
      var ty = y(thr[j][0]).toFixed(1);
      h += '<line x1="' + L + '" y1="' + ty + '" x2="' + R + '" y2="' + ty
        + '" stroke="' + thr[j][1] + '" stroke-width="1" stroke-dasharray="2 3"'
        + ' opacity=".85"/>';
    }
    for (var k = 0; k <= 6; k++) {
      var tt = t0 + (t1 - t0) * k / 6, xx = x(tt).toFixed(1);
      var anc = k === 0 ? 'start' : k === 6 ? 'end' : 'middle';
      h += '<line x1="' + xx + '" y1="' + T + '" x2="' + xx + '" y2="' + B
        + '" stroke="rgba(128,128,128,.16)"/>';
      h += '<text x="' + xx + '" y="' + (B + 15) + '" text-anchor="' + anc
        + '" font-size="10" fill="var(--muted)">' + tt.toFixed(1) + 's</text>';
    }
    h += slipLine(s, s.front, '#0d6efd', 1.3, x, y);
    h += slipLine(s, s.rear, '#7048e8', 1.3, x, y);
    h += '</svg>';
    h += '<div class="an-legend">'
      + '<span><i style="background:#0d6efd"></i>前轴滑移</span>'
      + '<span><i style="background:#7048e8"></i>后轴滑移</span>'
      + '<span><i style="background:#dc3545"></i>抱死阈值 −15%</span>'
      + '<span><i style="background:#f08c00"></i>空转阈值 +10%</span>'
      + '</div>';
    h += '<p class="an-note">纵轴 = 滑移率 <b>(ω·R − v) / v</b>：'
      + '<b>正值 = 轮子转得比车快（空转）</b>，<b>负值 = 比车慢（抱死）</b>。'
      + '曲线已截断到 ±100%（低速全油门空转实测能到 +798%），'
      + '峰值请以右侧事件表为准。</p>';
    return h;
  }

  function slipLapTable() {
    var rows = SLIP.laps || [];
    var h = '<div class="an-wrap" style="max-height:300px">'
      + '<table class="an-table"><thead><tr><th>圈</th><th>帧数</th>'
      + '<th style="text-align:right">前轴 p05</th>'
      + '<th style="text-align:right">前轴 min</th>'
      + '<th style="text-align:right">后轴 p95</th>'
      + '<th style="text-align:right">后轴 max</th>'
      + '<th style="text-align:right">抱死帧</th>'
      + '<th style="text-align:right">空转帧</th></tr></thead><tbody>';
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i], f = r.front || {}, b = r.rear || {};
      var hot = (r.lockup_frames || 0) + (r.wheelspin_frames || 0) > 0;
      h += '<tr' + (hot ? ' class="an-hot"' : '') + '><td>' + r.lap + '</td>'
        + '<td class="num" style="color:var(--muted)">' + r.frames + '</td>'
        + '<td class="num">' + pct(f.p05) + '</td>'
        + '<td class="num">' + pct(f.min) + '</td>'
        + '<td class="num">' + pct(b.p95) + '</td>'
        + '<td class="num">' + pct(b.max) + '</td>'
        + '<td class="num"' + (r.lockup_frames ? ' style="color:var(--bad);font-weight:600"' : '')
        + '>' + r.lockup_frames + '</td>'
        + '<td class="num"' + (r.wheelspin_frames ? ' style="color:#f08c00;font-weight:600"' : '')
        + '>' + r.wheelspin_frames + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  }

  function slipEvents(list, color, label) {
    if (!list || !list.length) {
      return '<p class="an-note">整场没有' + esc(label) + '。</p>';
    }
    var h = '<p class="an-note" style="margin-bottom:4px">最严重的 ' + list.length
      + ' 次' + esc(label) + '：</p><div class="an-wrap" style="max-height:260px">'
      + '<table class="an-table"><thead><tr><th>圈</th>'
      + '<th style="text-align:right">圈内时刻</th>'
      + '<th style="text-align:right">滑移率</th>'
      + '<th style="text-align:right">车速</th>'
      + '<th style="text-align:right">油门</th>'
      + '<th style="text-align:right">刹车</th>'
      + '<th style="text-align:right">持续</th></tr></thead><tbody>';
    for (var i = 0; i < list.length; i++) {
      var e = list[i];
      h += '<tr><td>' + e.lap + '</td>'
        + '<td class="num">' + (+e.t_rel).toFixed(2) + '</td>'
        + '<td class="num" style="color:' + color + ';font-weight:600">'
        + pct(e.slip) + '</td>'
        + '<td class="num">' + (+e.speed_kph).toFixed(1) + '</td>'
        + '<td class="num">' + Math.round(e.throttle) + '%</td>'
        + '<td class="num">' + Math.round(e.brake) + '%</td>'
        + '<td class="num" style="color:var(--muted)">' + e.frames + ' 帧</td></tr>';
    }
    return h + '</tbody></table></div>';
  }

  function renderSlipLap() {
    // 🔴 只更新自己那两个容器。之前这里直接写 #slipBody，把上面刚渲染好的
    //    标定统计和逐圈表整块覆盖掉了——卡片看着"渲染成功"，数字全没了。
    var cbox = el('slipChartBox'), ebox = el('slipEventBox');
    if (!SLIP) return;
    var lk = SLIP.lockup || {}, ws = SLIP.wheelspin || {};
    if (cbox) cbox.innerHTML = slipChart(slipLapNo);
    if (ebox) {
      ebox.innerHTML =
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:6px 18px;margin-top:10px">'
        + '<div>' + slipEvents(lk.worst, 'var(--bad)', '抱死（刹车到底把前轮刹停）') + '</div>'
        + '<div>' + slipEvents(ws.worst, '#f08c00', '空转（油门到底、后轮打滑）') + '</div>'
        + '</div>';
    }
  }

  window.slipPick = function (v) {
    slipLapNo = parseInt(v, 10) || slipLapNo;
    renderSlipLap();
  };

  function renderSlip() {
    var card = el('cardSlip'), box = el('slipBody'), meta = el('slipMeta');
    if (!card || !box) return;
    // 老场次（含合成的测试数据）没有 wheel_rads：整卡隐藏，不给空表
    if (!SLIP || SLIP.available === false) {
      card.style.display = 'none';
      return;
    }
    var c = SLIP.calibration || {};
    var lk = SLIP.lockup || {}, ws = SLIP.wheelspin || {};
    var laps = SLIP.laps || [];

    // 默认停在「最脏」的那一圈（抱死 + 空转帧数最多），比停在第一圈有用
    var pk = 0, pn = -1;
    for (var i = 0; i < laps.length; i++) {
      var v = (laps[i].lockup_frames || 0) + (laps[i].wheelspin_frames || 0);
      if (v > pn) { pn = v; pk = laps[i].lap; }
    }
    slipLapNo = pk || (laps.length ? laps[0].lap : 0);

    var sel = el('slipLapSel');
    if (sel) {
      sel.innerHTML = '';
      for (var j = 0; j < laps.length; j++) {
        var o = document.createElement('option');
        var bad = (laps[j].lockup_frames || 0) + (laps[j].wheelspin_frames || 0);
        o.value = laps[j].lap;
        o.textContent = '第 ' + laps[j].lap + ' 圈'
          + (bad ? '（滑移 ' + bad + ' 帧）' : '');
        if (laps[j].lap === slipLapNo) o.selected = true;
        sel.appendChild(o);
      }
    }
    if (meta) meta.textContent = '四轮角速度 ' + laps.length + ' 圈';

    var h = '<div class="an-stats">';
    h += stat('自标定半径', (+c.front_m).toFixed(4) + ' / ' + (+c.rear_m).toFixed(4) + ' m',
              '前轴 / 后轴 · 比值 ' + c.ratio);
    h += stat('自由滚动帧', c.free_frames + '（' + c.free_pct + '%）',
              // 标定残差在 1e-4 量级，用 1 位小数会显示成「-0.0%」看不出好坏
              '标定后滑移均值 ' + (+c.free_slip_front * 100).toFixed(2) + '% / '
              + (+c.free_slip_rear * 100).toFixed(2) + '%');
    h += stat('标定自洽', c.ok ? '✓ 通过' : '⚠ 未通过', c.reason || '滑移均值≈0，半径可信',
              c.ok ? 'var(--ok)' : 'var(--warn)');
    h += '</div>';
    h += '<p class="an-note">半径不靠任何外部参数，而是在<b>自由滚动帧</b>上现场标定'
      + '（松油、松刹、低速以上、纵向与横向 G 都小）：那时 <b>ω·R ≈ v</b>，'
      + '于是 <b>R = Σ(v·ω)/Σ(ω²)</b>。前后轴<b>分别</b>标定——实测比值 '
      + c.ratio + '，若强用同一个半径，滑移率会被整体偏置约 '
      + ((1 - c.ratio) * 100).toFixed(1) + '%，而抱死的典型信号本身只有百分之几。</p>';
    h += '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px">'
      + '<div class="an-stat" style="flex:1 1 160px"><span>抱死（轮子被刹停）</span>'
      + '<b style="color:var(--bad)">' + lk.events + ' 次</b><em>共 ' + lk.frames
      + ' 帧</em></div>'
      + '<div class="an-stat" style="flex:1 1 160px"><span>空转（后轮打滑）</span>'
      + '<b style="color:#f08c00">' + ws.events + ' 次</b><em>共 ' + ws.frames
      + ' 帧</em></div></div>';
    h += '<div id="slipChartBox"></div>';
    h += '<div id="slipEventBox"></div>';
    h += '<p class="an-note" style="margin-top:14px">逐圈滑移统计'
      + '（<b>抱死帧 / 空转帧</b> 不为 0 的圈已高亮；'
      + 'p05 / p95 是第 5 / 95 百分位，比极值更能代表常态）：</p>';
    h += slipLapTable();
    box.innerHTML = h;
    renderSlipLap();
  }

  // 🔴 取数失败与**渲染**失败必须分开处理。
  //    写成 `.then(render).catch(hide)` 的话，render 里抛的任何异常都会被
  //    当成"取不到数据"而把整张卡静默隐藏——页面看着正常，卡就没了，
  //    排查时完全看不到线索（这里踩过）。所以用双参数 then：
  //    只让 fetch/JSON 的失败走错误分支，渲染异常直接冒到 console。
  function secFetchFail() {
    var meta = el('secMeta'), box = el('secBody');
    if (meta) meta.textContent = '读取失败';
    if (box) box.innerHTML = '<p class="an-note">分段数据请求失败（见控制台）。</p>';
  }
  function slipFetchFail() {
    var meta = el('slipMeta'), box = el('slipBody');
    if (meta) meta.textContent = '读取失败';
    if (box) box.innerHTML = '<p class="an-note">滑移数据请求失败（见控制台）。</p>';
  }

  fetch(API + '/sectors?n=' + secN).then(function (r) { return r.json(); })
    .then(function (d) { SEC = d; renderSec(d); }, secFetchFail);

  fetch(API + '/slip').then(function (r) { return r.json(); })
    .then(function (d) { SLIP = d; renderSlip(); }, slipFetchFail);
})();
</script>
"""


# 场次详情页的「驾驶事件时间线」卡片。
#
# 数据来自 /api/v1/sessions/<名>/events（冷路径 ~10s，服务端记忆化），
# 与 sectors/slip 一样**展开时才取**。渲染三块：类型计数条、逐圈时间条
# （事件为彩色线段）、明细表。点时间条线段或表格行 → telePinAt(lap, t_rel)
# 让遥测卡切圈并钉住十字光标（联动接口在 _TELEMETRY_TMPL 里）。
_EVENTS_TMPL = r"""
<div class="card" id="cardEvents">
  <h2>驾驶事件时间线
    <span style="float:right;display:flex;gap:8px;align-items:center;text-transform:none">
      <span id="evMeta" style="font-weight:400;color:var(--muted)"></span>
      <select id="evLapSel" onchange="evPick(this.value)"
        style="font-weight:400;font-size:12px;padding:3px 7px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:inherit;font-family:inherit"></select>
    </span>
  </h2>
  <div id="evBody"><p class="an-note">正在检测事件…（首次约 10 秒）</p></div>
</div>

<script>
// ---------- 场次详情页 · 驾驶事件时间线 ----------
(function () {
  var FILE = '__FILE__';
  var API = '/api/v1/sessions/' + encodeURIComponent(FILE);
  var EV = null, evLap = 0;
  var TYPE_C = {
    collision: '#dc3545', spin: '#e8590c', off_track: '#f08c00',
    hard_braking: '#0d6efd', heavy_throttle: '#198754', tyre_abuse: '#7048e8'
  };

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function tname(t) {
    return (EV && EV.type_names && EV.type_names[t]) || t;
  }
  function tcol(t) { return TYPE_C[t] || '#888'; }
  function f2(v) { return (+v).toFixed(2); }

  function typeCounts(evts) {
    var m = {};
    for (var i = 0; i < evts.length; i++) {
      m[evts[i].type] = (m[evts[i].type] || 0) + 1;
    }
    return m;
  }

  function renderTypes(evts) {
    var m = typeCounts(evts);
    var keys = Object.keys(m).sort(function (a, b) { return m[b] - m[a]; });
    if (!keys.length) {
      return '<p class="an-note">这一圈<b>没有检测到事件</b>——阈值保守，'
        + '宁可漏报不误报；调整 <code>gt7-event-detector.py</code> 的 '
        + 'Thresholds 后重启生效。</p>';
    }
    var mx = 1;
    for (var k = 0; k < keys.length; k++) mx = Math.max(mx, m[keys[k]]);
    var h = '<div class="an-bars">';
    for (var i = 0; i < keys.length; i++) {
      var c = tcol(keys[i]);
      h += '<div class="an-bar-row"><span class="an-bar-lab">'
        + '<i class="an-sw" style="background:' + c + '"></i>' + esc(tname(keys[i]))
        + '</span><span class="an-bar"><i style="width:'
        + (m[keys[i]] / mx * 100).toFixed(1) + '%;background:' + c + '"></i></span>'
        + '<span class="an-bar-sum">' + m[keys[i]] + ' 次</span></div>';
    }
    return h + '</div>';
  }

  // 逐圈时间条：条全宽 = 该圈时长，事件画成彩色线段（点击 → 遥测联动）
  function renderLanes(evts) {
    var durs = (EV && EV.lap_durs) || {};
    var byLap = {};
    for (var i = 0; i < evts.length; i++) {
      (byLap[evts[i].lap] = byLap[evts[i].lap] || []).push(evts[i]);
    }
    var lapNos = Object.keys(byLap).map(Number).sort(function (a, b) { return a - b; });
    if (!lapNos.length) return '';
    var h = '';
    for (var li = 0; li < lapNos.length; li++) {
      var lap = lapNos[li];
      var dur = durs[lap] || 1;
      var list = byLap[lap];
      h += '<div class="an-bar-row"><span class="an-bar-lab">第 ' + lap
        + ' 圈 <span class="an-d">' + f2(dur) + 's</span></span>'
        + '<span class="an-bar ev-lane">';
      for (var j = 0; j < list.length; j++) {
        var e = list[j];
        var l = Math.min(100, e.t_rel / dur * 100);
        var w = Math.max(0.6, Math.min(100 - l, (e.t_end_rel - e.t_rel) / dur * 100));
        h += '<i class="ev-seg" style="left:' + l.toFixed(2) + '%;width:'
          + w.toFixed(2) + '%;background:' + tcol(e.type)
          + '" title="' + esc(tname(e.type) + ' · ' + f2(e.t_rel) + 's · '
            + Math.round(e.confidence * 100) + '%')
          + '" onclick="evPin(' + lap + ',' + e.t_rel + ')"></i>';
      }
      h += '</span><span class="an-bar-sum">' + list.length + ' 次</span></div>';
    }
    return h;
  }

  function evRow(e) {
    var ev = e.evidence || {};
    var parts = [];
    for (var k in ev) {
      if (Object.prototype.hasOwnProperty.call(ev, k)) {
        parts.push(esc(k) + ' <b>' + esc(JSON.stringify(ev[k])) + '</b>');
      }
    }
    return '<tr class="ev-row" onclick="evPin(' + e.lap + ',' + e.t_rel + ')">'
      + '<td class="num">第 ' + e.lap + ' 圈</td>'
      + '<td class="num">' + f2(e.t_rel) + '–' + f2(e.t_end_rel) + 's</td>'
      + '<td><i class="an-sw" style="background:' + tcol(e.type)
      + '"></i>' + esc(tname(e.type))
      + (e.src && e.src !== 'telemetry'
        ? ' <span class="an-d">(' + esc(e.src) + ')</span>' : '')
      + '</td>'
      + '<td class="num">' + Math.round(e.confidence * 100) + '%</td>'
      + '<td class="ev-ev">' + parts.join(' · ') + '</td>'
      + '<td class="an-d">' + esc(e.hint || '') + '</td></tr>';
  }

  function renderEvents() {
    var box = el('evBody'), meta = el('evMeta');
    if (!EV) return;
    var evts = (EV.events || []).filter(function (e) {
      return !evLap || e.lap === evLap;
    });
    if (meta) {
      meta.textContent = evLap ? ('第 ' + evLap + ' 圈 · ' + evts.length + ' 个事件')
        : ('全部 ' + (EV.laps || []).length + ' 圈 · ' + evts.length + ' 个事件');
    }
    var h = '<div class="an-stats">';
    var c = EV.calibration || {};
    h += '<div class="an-stat"><span>标定半径（前 / 后）</span>'
      + '<b>' + (c.available ? ((+c.front_m).toFixed(4) + ' / '
        + (+c.rear_m).toFixed(4) + ' m') : '兜底值') + '</b>'
      + '<em>' + esc(c.reason || '滑移率用带符号定义，与 /slip 同源') + '</em></div>';
    h += '</div>';
    h += renderTypes(evts);
    h += '<p class="an-note">时间条上每段是一种事件（<b>点一下</b>会跳到遥测曲线'
      + '对应时刻并钉住十字光标）；src = geometry 的出界由走线偏差判定，'
      + 'slip 是偏差不可信时的兜底。</p>';
    h += renderLanes(evts);
    if (evts.length) {
      h += '<div class="an-wrap"><table class="an-table"><thead><tr>'
        + '<th>圈</th><th>时刻（圈内秒）</th><th>类型</th><th>置信度</th>'
        + '<th>证据</th><th>提示</th></tr></thead><tbody>'
        + evts.map(evRow).join('') + '</tbody></table></div>';
    }
    box.innerHTML = h;
  }

  window.evPick = function (v) {
    evLap = parseInt(v, 10) || 0;
    renderEvents();
  };
  // 遥测卡联动入口（telePinAt 在 _TELEMETRY_TMPL 里定义）
  window.evPin = function (lap, tRel) {
    if (window.telePinAt) window.telePinAt(lap, tRel);
  };

  function fillLapSel() {
    var sel = el('evLapSel');
    if (!sel || !EV) return;
    var h = '<option value="0">全部圈</option>';
    var ls = EV.laps || [];
    for (var i = ls.length - 1; i >= 0; i--) {
      h += '<option value="' + ls[i] + '">第 ' + ls[i] + ' 圈</option>';
    }
    sel.innerHTML = h;
  }

  function evFetchFail() {
    var meta = el('evMeta'), box = el('evBody');
    if (meta) meta.textContent = '读取失败';
    if (box) box.innerHTML = '<p class="an-note">事件数据请求失败（见控制台）。</p>';
  }

  function boot() {
    fetch(API + '/events' + (evLap ? '?lap=' + evLap : ''))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        EV = d;
        // 检测器文件缺失 / 无帧：整卡隐藏，不给空表
        if (!EV || EV.available === false) {
          var card = el('cardEvents');
          if (card) card.style.display = 'none';
          return;
        }
        fillLapSel();
        // 🔴 与 _ANALYSIS_TMPL 同源：取数失败与渲染失败分开处理
        renderEvents();
      }, evFetchFail);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
<style>
.ev-lane { position: relative; min-height: 16px; }
.ev-seg { position: absolute; top: 2px; height: 12px; border-radius: 2px;
  cursor: pointer; opacity: .85; }
.ev-seg:hover { opacity: 1; box-shadow: 0 0 0 2px var(--card); }
.ev-row { cursor: pointer; }
.ev-row:hover td { background: rgba(128,128,128,.07) !important; }
.ev-ev { max-width: 420px; white-space: normal !important; font-size: 11.5px;
  color: var(--muted); }
.ev-ev b { color: var(--text); font-family: var(--mono); font-weight: 500; }
</style>
"""


# 场次详情页的「录像对齐」卡片（POV 解说 / 自动剪辑的时间轴锚点）。
#
# 为什么要有这张卡：/highlights 给的是**遥测时间轴**（相对本场第一个有效圈），
# 而 ffmpeg 要的是**录像时间轴**。两个时间轴差一个 offset，这个 offset 只有
# 玩家自己知道（什么时候按的录制），所以必须有个地方让他填一次并存下来。
#
# 三种填法，覆盖不同环境：
#   1. 「录像比遥测早开始 N 秒」——最直觉（录了 18 秒才发车就填 18）
#   2. 录像开始时刻——对着文件属性填
#   3. 自动探测——录像文件就在跑 dashboard 的这台机器上时用（ffprobe 优先，
#      退回 mtime 并明确警告 mtime 通常是"录完"的时刻）
_VIDEO_TMPL = r"""
<div class="card" id="cardVideo">
  <h2>录像对齐
    <span style="float:right;font-weight:400;font-size:12px;color:var(--muted)">
      剪辑 / 配音的时间轴锚点</span>
  </h2>
  <div id="vidBody"><p class="an-note">读取绑定信息…</p></div>
</div>

<script>
(function () {
  var FILE = '__FILE__';
  var API = '/api/v1/sessions/' + encodeURIComponent(FILE);
  var V = null, HL = null;
  // 按钮样式内联：这套 CSS 里没有 .btn，写个 class 会渲染成裸按钮
  var BS = 'style="border:1px solid var(--line);background:var(--card);'
    + 'color:inherit;border-radius:6px;padding:5px 12px;cursor:pointer;'
    + 'font-family:inherit;font-size:12px"';

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function fmt(v) { return (v == null) ? '—' : (+v).toFixed(1) + 's'; }

  function post(payload, cb) {
    fetch(API + '/video', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) { return r.json(); }).then(cb, function () {
      alert('请求失败');
    });
  }

  window.vidProbe = function () {
    var f = (el('vidFile') || {}).value || '';
    if (!f) { alert('先填录像文件路径'); return; }
    post({ file: f, probe: true }, function (d) {
      if (d.error) { alert(d.error); return; }
      if (d.warning) { alert(d.warning); }
      render(d);
    });
  };

  window.vidBind = function () {
    var f = ((el('vidFile') || {}).value || '').trim();
    if (!f) { alert('先填录像文件路径'); return; }
    var lead = ((el('vidLead') || {}).value || '').trim();
    var iso = ((el('vidIso') || {}).value || '').trim();
    if (!lead && !iso) {
      alert('填「录像比遥测早开始 N 秒」或「录像开始时刻」，二选一');
      return;
    }
    var p = { file: f };
    if (lead) { p.video_lead_s = parseFloat(lead); }
    else { p.video_start_iso = iso; }
    post(p, function (d) {
      if (d.error) { alert(d.error); return; }
      render(d);
    });
  };

  window.vidUnbind = function () {
    post({ clear: true }, function (d) {
      if (d.error) { alert(d.error); return; }
      render(null);
      load();
    });
  };

  function row(k, v) {
    return '<div><span>' + k + '</span><b>' + v + '</b></div>';
  }

  function render(d) {
    if (d) { V = d; }
    if (!V) { return; }
    if (!V.bound) {
      el('vidBody').innerHTML =
        '<p class="an-note">未绑定录像。绑定后，每段高光会额外给出'
        + '<b>录像里的第几秒</b>，ffmpeg 可以直接切。</p>'
        + formHtml('')
        + '<p style="font-size:12px;color:var(--muted);margin-top:10px">'
        + esc(V.session_start_iso || '') + ' = 遥测 0s（本场第一个有效圈的起点）'
        + '</p>';
      return;
    }
    var h = '<div class="kv">'
      + row('录像文件', '<span style="font-family:var(--mono);font-size:12px">'
            + esc(V.file) + '</span>')
      + row('遥测 0s', esc(V.session_start_iso || '—'))
      + row('录像起点', esc(V.video_start_iso || '—'))
      + row('录像早开始', fmt(V.video_lead_s))
      + row('换算', 't_video = t_session − ' + (+V.offset_s).toFixed(1) + 's')
      + '</div>';

    if (HL && HL.clips && HL.clips.length) {
      h += '<table style="margin-top:12px"><tr><th>#</th><th>事件</th>'
         + '<th style="text-align:right">遥测</th>'
         + '<th style="text-align:right">录像 ← 切这里</th></tr>';
      for (var i = 0; i < Math.min(HL.clips.length, 5); i++) {
        var c = HL.clips[i];
        h += '<tr><td>' + c.rank + '</td><td>' + esc(c.type_cn) + '（第 '
          + c.lap + ' 圈）</td><td class="num">' + fmt(c.clip_start) + '</td>'
          + '<td class="num"><b>' + fmt(c.clip_start_video) + '</b></td></tr>';
      }
      h += '</table><p style="font-size:12px;color:var(--muted);margin-top:8px">'
         + 'ffmpeg 示例：<code>ffmpeg -ss ' + fmt((HL.clips[0] || {}).clip_start_video)
         + ' -i "' + esc(V.file) + '" -t ' + ((HL.clips[0] || {}).duration || 0)
         + ' -c copy clip.mp4</code></p>';
    }
    h += formHtml(V.file)
       + '<p style="margin-top:8px"><button ' + BS + ' onclick="vidUnbind()">解绑</button></p>';
    el('vidBody').innerHTML = h;
  }

  function formHtml(f) {
    return '<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;'
      + 'align-items:flex-end">'
      + '<label style="font-size:12px;color:var(--muted)">录像文件路径<br>'
      + '<input id="vidFile" value="' + esc(f) + '" placeholder="E:/capture/race1.mp4" '
      + 'style="width:280px;font-family:var(--mono);font-size:12px;padding:5px 8px;'
      + 'border:1px solid var(--line);border-radius:6px;background:var(--card);'
      + 'color:inherit"></label>'
      + '<label style="font-size:12px;color:var(--muted)">比遥测早开始（秒）<br>'
      + '<input id="vidLead" placeholder="18" style="width:110px;font-family:var(--mono);'
      + 'font-size:12px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;'
      + 'background:var(--card);color:inherit"></label>'
      + '<label style="font-size:12px;color:var(--muted)">或 录像开始时刻<br>'
      + '<input id="vidIso" placeholder="2026-10-08 19:50:30" style="width:180px;'
      + 'font-family:var(--mono);font-size:12px;padding:5px 8px;'
      + 'border:1px solid var(--line);border-radius:6px;background:var(--card);'
      + 'color:inherit"></label>'
      + '<button ' + BS + ' onclick="vidBind()">绑定</button>'
      + '<button ' + BS + ' onclick="vidProbe()">自动探测</button>'
      + '</div>'
      + '<p style="font-size:12px;color:var(--muted);margin-top:8px">'
      + '「自动探测」只在录像文件<b>就在跑仪表盘的这台机器上</b>时有效'
      + '（Docker 部署时看不到你电脑的文件）；有 ffprobe 会用它反推开始时刻，'
      + '否则只能用文件修改时间，而那通常是<b>录完</b>的时刻。</p>';
  }

  function load() {
    fetch(API + '/video').then(function (r) { return r.json(); })
      .then(function (d) {
        V = d;
        render(d);
        // 绑了才去取高光（事件检测冷路径 ~10s，不值得为未绑定的场次付这个钱）
        if (d && d.bound) {
          fetch(API + '/highlights?top=5').then(function (r) { return r.json(); })
            .then(function (h) { HL = h; render(null); },
                  function () { HL = null; });
        }
      }, function () {
        el('vidBody').innerHTML = '<p class="an-note">读取失败</p>';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', load);
  } else { load(); }
})();
</script>
"""


# 场次详情页的「进站与名次」卡片。
#
# 数据来自 /api/v1/sessions/<名>/pitstops（全场帧遍历一遍 ~0.3s，服务端记忆化）。
# 三块：stint 表（进站切开的跑段）、名次时间线 SVG（quali_pos 逐帧实时名次）、
# 名次变化明细（超车/被超）。点变化点或明细行 → telePinAt 联动遥测。
# 🔴 轮胎磨损广播协议里没有（296 字节包无 wear 字段），所以这张卡不含
#    换胎判定——只有油量环跳给出的进站事实。
_PIT_TMPL = r"""
<div class="card" id="cardPit">
  <h2>进站与名次
    <span id="pitMeta" style="float:right;font-weight:400;color:var(--muted);text-transform:none"></span>
  </h2>
  <div id="pitBody"><p class="an-note">正在分析进站与名次…</p></div>
</div>

<script>
// ---------- 场次详情页 · 进站与名次 ----------
(function () {
  var FILE = '__FILE__';
  var API = '/api/v1/sessions/' + encodeURIComponent(FILE);
  var PIT = null;

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function f2(v) { return v == null ? '-' : (+v).toFixed(2); }
  function f1(v) { return v == null ? '-' : (+v).toFixed(1); }

  function stintTable() {
    var st = PIT.stints || [];
    if (!st.length) return '';
    var h = '<p class="an-note">按进站切开的跑段（stint）：'
      + '<b>均耗</b> = 该段油量差 ÷ 圈数；油量受引擎工况影响逐圈有波动，'
      + '这里取段内平均。</p>'
      + '<div class="an-wrap"><table class="an-table"><thead><tr>'
      + '<th>段</th><th>圈</th><th>圈数</th><th>时长</th>'
      + '<th>段首油量</th><th>段末油量</th><th>段内均耗</th></tr></thead><tbody>';
    for (var i = 0; i < st.length; i++) {
      var x = st[i];
      h += '<tr><td>' + x.stint + (x.after_stop ? ' <span class="an-d">进站后</span>' : '')
        + '</td>'
        + '<td class="num">' + x.from_lap + '–' + x.to_lap + '</td>'
        + '<td class="num">' + x.laps + '</td>'
        + '<td class="num">' + f1(x.dur_s) + 's</td>'
        + '<td class="num">' + f2(x.gas_start) + '</td>'
        + '<td class="num">' + f2(x.gas_end) + '</td>'
        + '<td class="num">' + (x.fuel_per_lap != null ? f2(x.fuel_per_lap) : '-')
        + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  }

  function pitList() {
    var ps = PIT.pitstops || [];
    if (!ps.length) {
      return '<p class="an-note">本场<b>没有检测到进站加油</b>'
        + '（油量全程单调下降）。换胎窗口无法从遥测判断——'
        + 'GT7 广播协议不含轮胎磨损字段。</p>';
    }
    var h = '<p class="an-note">检测到 <b>' + ps.length + ' 次进站</b>'
      + '（油量环跳 &gt;5% 判定；点行跳到遥测对应时刻）：</p>'
      + '<div class="an-wrap"><table class="an-table"><thead><tr>'
      + '<th>#</th><th>出站圈</th><th>时刻（圈内秒）</th>'
      + '<th>加油前</th><th>加油后</th></tr></thead><tbody>';
    for (var i = 0; i < ps.length; i++) {
      var p = ps[i];
      h += '<tr class="ev-row" onclick="pitPin(' + p.lap + ',' + p.t_rel + ')">'
        + '<td class="num">' + (i + 1) + '</td>'
        + '<td class="num">第 ' + p.lap + ' 圈</td>'
        + '<td class="num">' + f2(p.t_rel) + 's</td>'
        + '<td class="num">' + f2(p.before) + '</td>'
        + '<td class="num" style="color:var(--ok)">' + f2(p.after) + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  }

  // 名次时间线：x = 圈，y = 名次（P1 在顶）。
  // 🔴 名次来自 quali_pos(0x84)——比赛进行中它是**当前名次**（逐帧实时变），
  //    不是排位成绩。变化点：delta<0 超车（绿）/ >0 被超（红）。
  function posChart() {
    var evs = (PIT.pos_events || []).filter(function (e) {
      return e.from != null;
    });
    if (!evs.length) return '';
    var W = 1000, H = 150, PADL = 44, PADR = 16, PADT = 14, PADB = 24;
    var laps = PIT.laps_list || [];
    if (laps.length < 2) return '';
    var lmin = laps[0], lmax = laps[laps.length - 1];
    var mxPos = 1;
    for (var i = 0; i < evs.length; i++) {
      mxPos = Math.max(mxPos, evs[i].from, evs[i].pos);
    }
    function X(lap) { return PADL + (lap - lmin) / (lmax - lmin) * (W - PADL - PADR); }
    function Y(p) { return PADT + (p - 1) / Math.max(1, mxPos - 1) * (H - PADT - PADB); }
    var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" style="width:100%;height:auto">'
      + '<rect x="' + PADL + '" y="' + PADT + '" width="' + (W - PADL - PADR)
      + '" height="' + (H - PADT - PADB) + '" fill="rgba(128,128,128,.06)" rx="4"/>';
    for (var p = 1; p <= mxPos; p++) {
      s += '<line x1="' + PADL + '" y1="' + Y(p) + '" x2="' + (W - PADR)
        + '" y2="' + Y(p) + '" stroke="rgba(128,128,128,.15)" stroke-width="1"/>'
        + '<text x="' + (PADL - 6) + '" y="' + (Y(p) + 3) + '" font-size="10"'
        + ' text-anchor="end" fill="var(--muted)">P' + p + '</text>';
    }
    for (var k = 0; k < laps.length; k++) {
      var lv = laps[k];
      if ((lv - lmin) % Math.ceil((lmax - lmin) / 10) === 0) {
        s += '<text x="' + X(lv) + '" y="' + (H - 6) + '" font-size="10"'
          + ' text-anchor="middle" fill="var(--muted)">' + lv + '</text>';
      }
    }
    // 进站圈画竖虚线
    var ps = PIT.pitstops || [];
    for (var q = 0; q < ps.length; q++) {
      s += '<line x1="' + X(ps[q].lap) + '" y1="' + PADT + '" x2="' + X(ps[q].lap)
        + '" y2="' + (H - PADB) + '" stroke="var(--warn)" stroke-width="1"'
        + ' stroke-dasharray="3 3" opacity=".6"/>';
    }
    for (var j = 0; j < evs.length; j++) {
      var e = evs[j];
      var c = e.delta < 0 ? 'var(--ok)' : 'var(--bad)';
      s += '<line x1="' + X(e.lap) + '" y1="' + Y(e.from) + '" x2="' + X(e.lap)
        + '" y2="' + Y(e.pos) + '" stroke="' + c + '" stroke-width="2"/>'
        + '<circle cx="' + X(e.lap) + '" cy="' + Y(e.pos) + '" r="3.5" fill="' + c
        + '" stroke="var(--card)" stroke-width="1" class="pit-dot"'
        + ' data-lap="' + e.lap + '" data-t="' + e.t_rel + '"/>'
        + '<circle cx="' + X(e.lap) + '" cy="' + Y(e.from) + '" r="2.5" fill="none"'
        + ' stroke="' + c + '" stroke-width="1"/>';
    }
    return s + '</svg>';
  }

  function posTable() {
    var evs = (PIT.pos_events || []).filter(function (e) {
      return e.from != null;
    });
    if (!evs.length) {
      return '<p class="an-note">全场名次没有变化。</p>';
    }
    var h = '<p class="an-note">名次变化明细（<b style="color:var(--ok)">超车</b> / '
      + '<b style="color:var(--bad)">被超</b>；点行跳到遥测对应时刻）：</p>'
      + '<div class="an-wrap" style="max-height:220px"><table class="an-table"><thead><tr>'
      + '<th>圈</th><th>时刻</th><th>变化</th><th>类型</th></tr></thead><tbody>';
    for (var i = 0; i < evs.length; i++) {
      var e = evs[i];
      var over = e.delta < 0;
      h += '<tr class="ev-row" onclick="pitPin(' + e.lap + ',' + e.t_rel + ')">'
        + '<td class="num">第 ' + e.lap + ' 圈</td>'
        + '<td class="num">' + f2(e.t_rel) + 's</td>'
        + '<td class="num">P' + e.from + ' → P' + e.pos + '</td>'
        + '<td style="color:' + (over ? 'var(--ok)' : 'var(--bad)') + '">'
        + (over ? '▲ 超车' : '▼ 被超') + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  }

  window.pitPin = function (lap, tRel) {
    if (window.telePinAt) window.telePinAt(lap, tRel);
  };

  function render() {
    var card = el('cardPit'), box = el('pitBody'), meta = el('pitMeta');
    if (!card || !box) return;
    if (!PIT || PIT.available === false) {
      card.style.display = 'none';
      return;
    }
    var ps = PIT.pitstops || [];
    if (meta) {
      meta.textContent = (PIT.powertrain === 'electric' ? '电车（不检测加油） · ' : '')
        + ps.length + ' 次进站 · ' + (PIT.laps_list || []).length + ' 圈';
    }
    box.innerHTML = pitList() + stintTable()
      + '<p class="an-note" style="margin-top:12px">名次时间线（虚线 = 进站圈；'
      + '名次来自比赛中的实时排名，点变化点跳遥测）：</p>'
      + posChart() + posTable();
  }

  function pitFetchFail() {
    var meta = el('pitMeta'), box = el('pitBody');
    if (meta) meta.textContent = '读取失败';
    if (box) box.innerHTML = '<p class="an-note">进站数据请求失败（见控制台）。</p>';
  }

  function boot() {
    fetch(API + '/pitstops').then(function (r) { return r.json(); })
      .then(function (d) { PIT = d; render(); }, pitFetchFail);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }
})();
</script>
"""


# 场次详情页的「遥测数据」卡片：逐帧原始数据可视化 + 分页表 + CSV 导出。
#
# 背景：接收器本来就逐帧把四十几个字段写进了 jsonl，但旧版详情页只把它
# 汇总成「最高速度 / 平均速度」几个数字，用户看不到「第 37.5 秒、时速 182、
# 油门 63%、刹车 0%」这种逐帧数据——等于存了看不见。
#
# 数据不内嵌进页面，走 /api/v1/sessions/<名>/{series,frames,csv}：
#   · 单场 jsonl 能到 250MB，内嵌会把首屏拖垮；
#   · 切圈 / 翻页本来就要重新取数，走接口只有一条代码路径。
_TELEMETRY_TMPL = r"""
<div class="card">
  <h2>遥测数据
    <span style="float:right;display:flex;gap:8px;align-items:center;text-transform:none">
      <span id="teleMeta" style="font-weight:400;color:var(--muted)"></span>
      <select id="teleLapSel" onchange="teleSetLap(this.value)"
        style="font-weight:400;font-size:12px;padding:3px 7px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:inherit;font-family:inherit"></select>
      <button id="teleZoom" class="tbtn" onclick="openZoom('teleChart')">⤢ 放大曲线</button>
      <a id="teleCsv" class="tbtn" href="#" download>⬇ 导出 CSV</a>
    </span>
  </h2>
  <p style="font-size:12px;color:var(--muted);margin-bottom:10px">
    接收器<b>逐帧</b>落盘的原始通道都在这里：时间、速度、转速、档位、油门 / 刹车百分比、
    横向 / 纵向 G、油量。曲线默认跨整场，右上角可切到某一圈；
    帧数很多时会自动<b>等间隔抽稀</b>后再画（是抽稀不是丢帧，曲线依然覆盖全程）。
    <b>鼠标划过曲线</b>会跟着出现十字光标，<b>点一下</b>就把读数钉住（再点一下取消，
    或按 Esc），下方读数条会列出该时刻每项通道的具体数值；放大曲线请点右上角按钮，
    下方表格可翻页看原始帧。
    横轴与「时间」列都是<b>相对时间</b>：整场 = 从场次开始算，单圈 = 从该圈起点算，
    导出的 CSV 也是同一基准。
  </p>
  <div id="teleChartWrap" class="telewrap2">
    <div id="teleChart" class="telechart"></div>
    <svg id="teleCursor" class="telecursor" aria-hidden="true"></svg>
  </div>
  <div id="teleReadout" class="treadout"></div>
  <div class="telebar">
    <b style="font-size:12px">原始帧</b>
    <span id="telePageInfo" style="font-size:12px;color:var(--muted)"></span>
    <span style="flex:1"></span>
    <button class="tbtn" onclick="telePage(-1)">← 上一页</button>
    <button class="tbtn" onclick="telePage(1)">下一页 →</button>
  </div>
  <div class="telewrap">
    <table class="teletable"><thead id="teleHead"></thead><tbody id="teleBody"></tbody></table>
  </div>
</div>
<style>
.telechart svg { display:block; width:100%; height:auto; cursor:crosshair; }
/* 十字光标是**叠在曲线上的独立 SVG**：只在移鼠标时重画这几条线，
   不重绘 2000+ 个点的曲线（那会卡）。pointer-events:none 让事件穿透到下面的曲线。 */
.telewrap2 { position:relative; }
.telecursor { position:absolute; left:0; top:0; pointer-events:none; display:none; }
.telecursor.on { display:block; }
.treadout { margin-top:8px; padding:7px 10px; border:1px solid var(--line);
  border-radius:8px; background:rgba(128,128,128,.06); display:flex;
  flex-wrap:wrap; gap:6px 14px; align-items:baseline; font-size:12px; min-height:32px; }
.treadout .trhint { color:var(--muted); font-size:11.5px; }
.treadout .tr-item { display:inline-flex; align-items:baseline; gap:4px; white-space:nowrap; }
.treadout .tr-item i { font-style:normal; color:var(--muted); font-size:11px; }
.treadout .tr-item b { font-family:var(--mono); font-size:13px; font-weight:600; }
.treadout .tr-item em { font-style:normal; color:var(--muted); font-size:10.5px; }
.treadout .tr-pin { border:1px solid var(--line); border-radius:5px; padding:1px 7px;
  cursor:pointer; font-size:11px; color:var(--muted); }
.treadout .tr-pin:hover { background:rgba(128,128,128,.18); }
.treadout.pinned { border-color:var(--accent, #0d6efd); }
.tbtn { border:1px solid var(--line); background:var(--card); color:inherit;
  border-radius:6px; padding:3px 9px; cursor:pointer; font-family:inherit;
  font-size:12px; text-decoration:none; display:inline-block; }
.tbtn:hover { background:rgba(128,128,128,.16); }
.telebar { display:flex; align-items:center; gap:8px; margin:12px 0 6px; }
.telewrap { max-height:420px; overflow:auto; border:1px solid var(--line);
  border-radius:8px; }
.teletable { width:100%; border-collapse:collapse; font-size:12px; }
.teletable th, .teletable td { padding:4px 8px;
  border-bottom:1px solid var(--line); white-space:nowrap; }
.teletable th { position:sticky; top:0; background:var(--card);
  color:var(--muted); font-weight:500; font-size:11px; z-index:1; }
.teletable td.num { font-family:var(--mono); }
.teletable tr:hover td { background:rgba(128,128,128,.06); cursor:default; }
</style>
<script>
// ---------- 场次详情页 · 遥测数据 ----------
(function () {
  var FILE = '__FILE__';
  var API = '/api/v1/sessions/' + encodeURIComponent(FILE);
  // 列下标与后端 _SERIES_COLS 一一对应，改动必须两边一起改
  var IDX = {t:0, spd:1, rpm:2, thr:3, brk:4, gear:5, glat:6, glon:7, fuel:8, lap:9};
  // 表格列（顺序即显示顺序）
  var HEAD = [
    ['t',    '时间(s)'],
    ['spd',  '速度(km/h)'],
    ['rpm',  '转速'],
    ['gear', '档位'],
    ['thr',  '油门(%)'],
    ['brk',  '刹车(%)'],
    ['glat', '横向G'],
    ['glon', '纵向G'],
    ['fuel', '油量(%)'],
    ['lap',  '圈']
  ];
  // 曲线的分面配置：
  //   fix    = 固定量程（踏板有明确的 0~100 物理含义，直接钉死）
  //   sym    = 以 0 对称（G 力可正可负）
  //   bounds = 物理边界：自动量程的余量**不许越过**它（否则油量轴会显示成 -3~106）
  var FACETS = [
    {k:'spd',  label:'速度',   unit:'km/h', c:'#0d6efd'},
    {k:'rpm',  label:'转速',   unit:'rpm',  c:'#e8590c'},
    {k:'thr',  label:'油门',   unit:'%',    c:'#198754', fix:[0,100]},
    {k:'brk',  label:'刹车',   unit:'%',    c:'#dc3545', fix:[0,100]},
    {k:'gear', label:'档位',   unit:'',     c:'#7048e8'},
    {k:'glon', label:'纵向 G', unit:'g',    c:'#c2255c', sym:true},
    {k:'glat', label:'横向 G', unit:'g',    c:'#0c8599', sym:true},
    {k:'fuel', label:'油量',   unit:'%',    c:'#f08c00', bounds:[0,100]}
  ];
  var pageSize = 200, pageOff = 0, curLap = 0, rows = [], laps = [], ready = false;
  // 十字光标要用与曲线完全相同的 Y 换算，所以把每个分面的量程/像素范围存下来
  var scales = [], geom = null, cxRow = null, cxPinned = false;
  // 事件时间线卡的联动钉点：telePinAt 设进来，series 到手后消费掉（见 loadSeries）
  var pendingPin = null;

  function gv(n, fb) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(n).trim();
    return v || fb;
  }
  function el(id) { return document.getElementById(id); }
  function fmtCell(k, v) {
    if (v === null || v === undefined || v === '') return '-';
    if (k === 'glat' || k === 'glon') return (+v).toFixed(2);
    if (k === 't') return (+v).toFixed(2);
    if (k === 'thr' || k === 'brk' || k === 'fuel') return String(Math.round(v));
    return String(Math.round(v));
  }
  function fmtTick(f, v) {
    if (f.sym) return (v > 0 ? '+' : '') + (v === 0 ? '0' : (+v).toFixed(1));
    if (f.k === 'rpm') return String(Math.round(v));
    if (f.unit === 'g') return (+v).toFixed(1);
    return String(Math.round(v));
  }

  // —— 全通道分面曲线 ——
  // 每个通道独立 Y 轴：速度 0~250、转速 0~9000、踏板 0~100 量纲差两个数量级，
  // 共用一把尺只会得到「油门线爬满格」的尺度假象。
  function renderChart() {
    var box = el('teleChart');
    if (!box) return;
    // 任何一次重画（切圈 / 缩放窗口 / 数据到达）都作废旧的光标读数
    cxClear();
    if (!ready) { box.innerHTML = '<div style="padding:26px;text-align:center;color:var(--muted)">正在读取逐帧数据…</div>'; return; }
    if (!rows.length) { box.innerHTML = '<div style="padding:26px;text-align:center;color:var(--muted)">这一范围没有逐帧数据</div>'; return; }
    var W = Math.max(360, box.clientWidth || 720);
    var PADL = 4, PADR = 48, PW = W - PADL - PADR;
    var FH = 56, GAP = 12, XLH = 20;
    var t0 = rows[0][IDX.t], t1 = rows[rows.length - 1][IDX.t];
    var span = Math.max(t1 - t0, 0.001);
    var grid = gv('--cv-grid', 'rgba(128,128,128,.18)');
    var tcol = gv('--cv-text', 'rgba(128,128,128,.6)');
    function X(t) { return PADL + PW * (t - t0) / span; }
    scales = [];

    var total = FACETS.length * (FH + GAP) + XLH;
    var s = '<svg width="' + W + '" height="' + total + '" viewBox="0 0 ' + W + ' ' + total + '">';
    for (var fi = 0; fi < FACETS.length; fi++) {
      var f = FACETS[fi], yi = IDX[f.k];
      var vals = [], has = false;
      for (var i = 0; i < rows.length; i++) {
        var v = rows[i][yi];
        if (v === null || v === undefined) continue;
        has = true; vals.push(+v);
      }
      if (!has) continue;
      var mn, mx;
      if (f.fix) { mn = f.fix[0]; mx = f.fix[1]; }
      else if (f.sym) {
        var a = 0.5;
        for (var j = 0; j < vals.length; j++) a = Math.max(a, Math.abs(vals[j]));
        mn = -a; mx = a;
      } else {
        mn = vals[0]; mx = vals[0];
        for (var j2 = 0; j2 < vals.length; j2++) {
          if (vals[j2] < mn) mn = vals[j2];
          if (vals[j2] > mx) mx = vals[j2];
        }
        // 🔴 有物理量程的量（油量 0~100%）：数据整个落在量程内时，直接用
        //    物理量程当坐标轴。百分比就该按 0~100 看，这样两个坑一起躲开：
        //      ① LM55 那场油量 2.83~100，留 6% 余量后轴变成 -3~106，
        //         用户看到「油量上限 106」以为数据坏了；
        //      ② 恒 100%（不耗油的车）走「数据无波动」兜底 → 上界 101。
        //    数据真的越界（加满溢出、传感器跳变）时才退回自动量程，不藏数据。
        if (f.bounds && mn >= f.bounds[0] && mx <= f.bounds[1]) {
          mn = f.bounds[0]; mx = f.bounds[1];
        } else {
          if (mx - mn < 1e-6) { mx = mn + 1; }
          // 上下各留 6% 余量，线不贴边
          var pad = (mx - mn) * 0.06; mn -= pad; mx += pad;
        }
      }
      var top = fi * (FH + GAP) + 13, bot = top + FH - 16;
      scales.push({k: f.k, yi: yi, mn: mn, mx: mx, top: top, bot: bot, f: f});
      // ⚠️ 用函数表达式而不是块内 function 声明：块级函数声明在不同
      //    JS 引擎/严格模式下的作用域规则不一致，这里是循环体内，别踩。
      var Y = function (v) { return bot - (bot - top) * ((v - mn) / (mx - mn)); };
      // 网格 + 右侧刻度
      for (var k = 0; k < 3; k++) {
        var tv = mn + (mx - mn) * k / 2;
        var gy = Y(tv).toFixed(1);
        s += '<line x1="' + PADL + '" y1="' + gy + '" x2="' + (PADL + PW)
          + '" y2="' + gy + '" stroke="' + grid + '" stroke-width="1"'
          + (k === 0 ? '' : ' stroke-dasharray="3 3"') + '/>';
        s += '<text x="' + (PADL + PW + 5) + '" y="' + (+gy + 3.5)
          + '" font-size="10" fill="' + tcol + '" data-k="' + f.k + '">'
          + fmtTick(f, tv) + '</text>';
      }
      // 通道名（左上角，用本通道颜色，省掉一整个图例）
      s += '<text x="' + (PADL + 2) + '" y="' + (top - 5) + '" font-size="10.5"'
        + ' font-weight="600" fill="' + f.c + '">' + f.label
        + (f.unit ? ' <tspan font-weight="400">' + f.unit + '</tspan>' : '') + '</text>';
      // 数据线（缺值断开，不画成直线连过去）
      var d = '', pen = false;
      for (var q = 0; q < rows.length; q++) {
        var vv = rows[q][yi];
        if (vv === null || vv === undefined) { pen = false; continue; }
        d += (pen ? 'L' : 'M') + X(rows[q][IDX.t]).toFixed(1) + ',' + Y(+vv).toFixed(1);
        pen = true;
      }
      s += '<path d="' + d + '" fill="none" stroke="' + f.c
        + '" stroke-width="1.4" stroke-linejoin="round"/>';
    }
    // 时间轴（共用一条，标 5 个刻度）
    // ⚠️ 刻度值用「相对当前范围起点」的秒数：选了第 3 圈还标 162.7s 会让人
    //    以为是场次时间，读圈内节奏（入弯/出弯在第几秒）就完全对不上。
    //    x 坐标本身用的就是同一个 t0，所以相对化后刻度与曲线位置一致。
    var axY = FACETS.length * (FH + GAP) + 13;
    for (var m = 0; m <= 4; m++) {
      var tv2 = span * m / 4;
      var xx = X(t0 + tv2);
      s += '<text x="' + xx.toFixed(1) + '" y="' + axY + '" font-size="10" fill="'
        + tcol + '" text-anchor="' + (m === 0 ? 'start' : (m === 4 ? 'end' : 'middle'))
        + '">' + tv2.toFixed(1) + 's</text>';
    }
    s += '</svg>';
    box.innerHTML = s;
    // 十字光标要和曲线共用同一套坐标，这里把几何参数与量程留成模块级状态
    geom = {W: W, H: total, PADL: PADL, PW: PW, t0: t0, span: span};
    bindCursor(box);
  }

  // —— 十字光标 + 读数条 ——
  // 🔴 坐标换算一律走 getBoundingClientRect：SVG 里每个子节点（path/text）的
  //    offsetX 基准不一致，直接读 offsetX 会偏。用外框算再按 viewBox 比例还原。
  function bindCursor(box) {
    if (box._cxBound) return;
    box._cxBound = true;
    box.addEventListener('mousemove', function (e) {
      if (cxPinned) return;              // 钉住后不再跟随鼠标，方便读数
      cxAt(e.clientX);
    });
    box.addEventListener('mouseleave', function () {
      if (!cxPinned) cxClear();
    });
    box.addEventListener('click', function (e) {
      if (cxPinned) { cxClear(); return; }
      cxAt(e.clientX);
      cxPinned = true;
      renderReadout();
    });
    document.addEventListener('keydown', function (e) {
      if ((e.key === 'Escape' || e.keyCode === 27) && cxRow !== null) cxClear();
    });
  }

  // rows 按时间升序 → 二分找离 t 最近的采样点（不是插值：读数必须是真实落盘的帧）
  function nearestRow(t) {
    var lo = 0, hi = rows.length - 1;
    while (lo < hi) {
      var mid = (lo + hi) >> 1;
      if (rows[mid][IDX.t] < t) lo = mid + 1; else hi = mid;
    }
    if (lo > 0 && Math.abs(rows[lo - 1][IDX.t] - t) < Math.abs(rows[lo][IDX.t] - t)) lo--;
    return lo;
  }

  function cxAt(clientX) {
    var box = el('teleChart');
    if (!box || !geom || !rows.length) return;
    var svg = box.querySelector('svg');
    if (!svg) return;
    var r = svg.getBoundingClientRect();
    if (!r.width) return;
    var x = (clientX - r.left) * (geom.W / r.width);
    var t = geom.t0 + (x - geom.PADL) / geom.PW * geom.span;
    cxRow = nearestRow(t);
    // 光标吸附到真实采样点上，避免「线在两点之间、数值却是其中一点」的错位感
    drawCursor(geom.PADL + geom.PW * (rows[cxRow][IDX.t] - geom.t0) / geom.span);
    renderReadout();
  }

  function drawCursor(x) {
    var cv = el('teleCursor');
    if (!cv || !geom || cxRow === null) return;
    var row = rows[cxRow];
    var accent = gv('--accent', '#0d6efd');
    var s = '<line x1="' + x.toFixed(1) + '" y1="3" x2="' + x.toFixed(1) + '" y2="'
      + (geom.H - 21) + '" stroke="' + accent
      + '" stroke-width="1" stroke-dasharray="4 3" opacity=".8"/>';
    for (var i = 0; i < scales.length; i++) {
      var sc = scales[i], v = row[sc.yi];
      if (v === null || v === undefined) continue;
      var y = sc.bot - (sc.bot - sc.top) * ((+v - sc.mn) / (sc.mx - sc.mn));
      // 每个分面各画一条横线 → 合起来就是「十字」
      s += '<line x1="' + geom.PADL + '" y1="' + y.toFixed(1) + '" x2="'
        + (geom.PADL + geom.PW) + '" y2="' + y.toFixed(1) + '" stroke="' + sc.f.c
        + '" stroke-width="1" stroke-dasharray="2 4" opacity=".5"/>';
      s += '<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="3" fill="'
        + sc.f.c + '" stroke="' + gv('--card', '#fff') + '" stroke-width="1.2"/>';
    }
    var tx = Math.min(Math.max(x, 24), geom.W - 24);
    s += '<text x="' + tx.toFixed(1) + '" y="' + (geom.H - 5)
      + '" font-size="10" font-weight="600" text-anchor="middle" fill="' + accent + '">'
      + rows[cxRow][IDX.t].toFixed(2) + 's</text>';
    cv.setAttribute('viewBox', '0 0 ' + geom.W + ' ' + geom.H);
    cv.setAttribute('width', geom.W);
    cv.setAttribute('height', geom.H);
    cv.innerHTML = s;
    cv.classList.add('on');
  }

  function renderReadout() {
    var box = el('teleReadout');
    if (!box) return;
    if (cxRow === null) {
      box.className = 'treadout';
      box.innerHTML = '<span class="trhint">把鼠标移到曲线上、或点一下曲线，'
        + '就会出十字光标并列出该时刻的各项数值</span>';
      return;
    }
    var row = rows[cxRow];
    var out = '<span class="tr-item"><i>时间</i><b>' + fmtCell('t', row[IDX.t])
      + '</b><em>s</em></span>';
    for (var i = 0; i < FACETS.length; i++) {
      var f = FACETS[i], v = row[IDX[f.k]];
      out += '<span class="tr-item"><i>' + f.label + '</i><b style="color:' + f.c
        + '">' + fmtCell(f.k, v) + '</b>'
        + (f.unit ? '<em>' + f.unit + '</em>' : '') + '</span>';
    }
    out += '<span class="tr-item"><i>圈</i><b>' + (row[IDX.lap] || '-') + '</b></span>';
    out += cxPinned
      ? '<span class="tr-pin" onclick="teleCxClear()">✕ 取消钉住</span>'
      : '<span class="trhint">点一下曲线可钉住</span>';
    box.className = 'treadout' + (cxPinned ? ' pinned' : '');
    box.innerHTML = out;
  }

  function cxClear() {
    cxRow = null; cxPinned = false;
    var cv = el('teleCursor');
    if (cv) { cv.classList.remove('on'); cv.innerHTML = ''; }
    renderReadout();
  }
  window.teleCxClear = cxClear;

  // —— 逐帧表格（服务端分页）——
  function renderTable(data) {
    var head = el('teleHead'), body = el('teleBody');
    if (!head || !body) return;
    if (!head.innerHTML) {
      head.innerHTML = '<tr>' + HEAD.map(function (h) {
        return '<th>' + h[1] + '</th>';
      }).join('') + '</tr>';
    }
    body.innerHTML = (data.rows || []).map(function (r, i) {
      return '<tr>' + HEAD.map(function (h) {
        var ci = IDX[h[0]];
        return '<td class="num">' + fmtCell(h[0], r[ci]) + '</td>';
      }).join('') + '</tr>';
    }).join('') || '<tr><td colspan="' + HEAD.length
      + '" style="text-align:center;color:var(--muted)">无数据</td></tr>';
    var from = data.total ? data.offset + 1 : 0;
    var to = Math.min(data.offset + data.limit, data.total);
    el('telePageInfo').textContent = '第 ' + from + '–' + to + ' 帧 / 共 '
      + data.total + ' 帧' + (curLap ? '（第 ' + curLap + ' 圈）' : '');
  }

  function loadTable() {
    var u = API + '/frames?offset=' + pageOff + '&limit=' + pageSize
      + (curLap ? '&lap=' + curLap : '');
    return fetch(u).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { el('telePageInfo').textContent = d.error; return; }
      renderTable(d);
    }).catch(function () {
      el('telePageInfo').textContent = '读取失败';
    });
  }

  window.telePage = function (dir) {
    var next = pageOff + dir * pageSize;
    if (next < 0) return;
    pageOff = next;
    loadTable();
  };

  window.teleSetLap = function (v) {
    curLap = parseInt(v, 10) || 0;
    pageOff = 0;
    loadSeries();
    loadTable();
  };

  // 事件时间线卡 → 遥测曲线的联动入口：切到事件所在圈（t_rel 与
  // /series?lap=N 同一基准，都是圈内秒），等 series 到手后钉住十字光标。
  // lapT0 = 该圈起点相对场次起点的秒数：整场视图下 series 的 t 从场次
  // 起点算，需要把圈内秒换算过去（laps 由 loadSeries 填充）。
  window.telePinAt = function (lap, tRel) {
    lap = parseInt(lap, 10) || 0;
    var lapT0 = null;
    for (var i = 0; i < laps.length; i++) {
      if (laps[i].lap === lap) { lapT0 = laps[i].t0; break; }
    }
    pendingPin = { lap: lap, t: tRel, lapT0: lapT0 };
    if (curLap !== lap) {
      curLap = lap;
      pageOff = 0;
      loadSeries();
      loadTable();
    } else {
      // 已在目标圈：直接用现有 rows 钉（loadSeries 里那支不会走）
      pendingPin = null;
      if (rows.length && geom) {
        var tt = tRel;
        if (!curLap && lapT0 != null) tt = lapT0 + tRel;
        cxRow = nearestRow(tt);
        cxPinned = true;
        drawCursor(geom.PADL + geom.PW
          * (rows[cxRow][IDX.t] - geom.t0) / geom.span);
        renderReadout();
      }
    }
  };

  function loadSeries() {
    ready = false;
    renderChart();
    var u = API + '/series?max_points=2400' + (curLap ? '&lap=' + curLap : '');
    return fetch(u).then(function (r) { return r.json(); }).then(function (d) {
      ready = true;
      if (d.error) {
        rows = [];
        el('teleMeta').textContent = d.error;
        renderChart();
        return;
      }
      rows = d.rows || [];
      laps = d.laps || [];
      var sel = el('teleLapSel');
      if (sel && !sel.options.length) {
        var best = null;
        for (var i = 0; i < laps.length; i++) {
          if (best === null || laps[i].dur < best) best = laps[i].dur;
        }
        var h = '<option value="0">整场</option>';
        for (var j = laps.length - 1; j >= 0; j--) {
          h += '<option value="' + laps[j].lap + '">第 ' + laps[j].lap + ' 圈 · '
            + laps[j].dur.toFixed(2) + 's'
            + (laps[j].dur === best ? ' ★最快' : '') + '</option>';
        }
        sel.innerHTML = h;
      }
      var step = d.step || 1;
      // 用 scope_frames（当前范围）而不是 total_frames（整场）——
      // 选了第 3 圈还写整场的帧数会让人以为圈数据丢了。
      el('teleMeta').textContent = (curLap ? '第 ' + curLap + ' 圈 · ' : '整场 · ')
        + '共 ' + (d.scope_frames || d.total_frames || 0) + ' 帧 · 画了 '
        + (d.rows || []).length + ' 点' + (step > 1 ? '（每 ' + step + ' 帧取 1）' : '');
      var csv = el('teleCsv');
      if (csv) csv.href = API + '/csv' + (curLap ? '?lap=' + curLap : '');
      renderChart();
      // 事件时间线卡的联动：series 落地后把十字光标钉到指定时刻。
      // 🔴 必须在这里做而不是 telePinAt 里直接设 cxRow——切圈后 rows 还没到。
      if (pendingPin) {
        var pin = pendingPin; pendingPin = null;
        if (!pin.lap || pin.lap === curLap) {
          var tt = pin.t;
          if (!curLap && pin.lapT0 != null) tt = pin.lapT0 + pin.t;
          cxRow = nearestRow(tt);
          cxPinned = true;
          drawCursor(geom ? geom.PADL + geom.PW
            * (rows[cxRow][IDX.t] - geom.t0) / geom.span : 0);
          renderReadout();
        }
      }
    }).catch(function () {
      ready = true;
      rows = [];
      renderChart();
      el('teleMeta').textContent = '读取失败';
    });
  }

  function boot() {
    loadSeries();
    loadTable();
    // 🔴 这里**不**调 makeZoomable：曲线上的单击已经被「钉住十字光标读数」占用，
    //    同一张图上再绑「点击放大」就成了一个点击两个动作。
    //    放大改到 h2 工具条上的「⤢ 放大曲线」按钮（直接调 openZoom）。
    if (window.registerZoom) {
      registerZoom('teleChart', {
        title: '遥测数据 · 全通道曲线', kind: 'node', src: 'teleChart'
      });
    }
    renderReadout();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else { boot(); }

  // 窗口变宽变窄要重画（SVG 是按容器像素宽拼的，不是 viewBox 自适应）
  var rt = null;
  window.addEventListener('resize', function () {
    clearTimeout(rt);
    rt = setTimeout(renderChart, 180);
  });
})();
</script>
"""


def build_sessions_page(hist: Path) -> str:
    """历史场次列表页。空状态要说清为什么空、以及怎么让它有数据。"""
    import html as _html
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
      <td style="font-size:12.5px">{_html.escape(s.get('car_name') or '') or '<span class="dim">-</span>'
          }{'<span class="dim"> · ' + _html.escape(s['track_name']) + '</span>' if s.get('track_name') else ''}</td>
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


def build_session_page(path: Path, stats: dict, ref_lap_no: int | None = None,
                       cmp_lap_no: int | None = None) -> str:
    """单场次详情：把离线统计展示成人能读的页面。"""
    import html as _html
    try:
        cmp_data = compare_session(path, ref_lap_no=ref_lap_no,
                                   cmp_lap_no=cmp_lap_no)
    except Exception:
        cmp_data = {}
    if isinstance(cmp_data, dict):
        # 前端「行车轨迹」卡片要按圈单独拉赛车线（不重算整页），得知道是哪一场。
        # 塞进 CMP 里比再加一个字符串占位符省事，也不会被 JSON 转义搞坏。
        cmp_data["file"] = path.name
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

    # —— 赛道自动识别：详情页打开即识别一次并落库（之后走缓存快路径）——
    # 识别失败（无有效圈）不阻塞页面，赛道行显示「未识别」。
    trk = session_track(path)
    if trk.get("track_id") is not None:
        trk_label = _html.escape(trk.get("name") or f"未命名赛道 #{trk['track_id']}")
        trk_title = (f"指纹距离 {trk.get('distance')}" if trk.get("matched")
                     else "场次较少未匹配到已知赛道；点 ✎ 给它起名")
        trk_html = (f'{trk_label} '
                    f'<button title="{trk_title}" style="border:1px solid var(--line);'
                    f'background:var(--card);color:inherit;border-radius:6px;'
                    f'padding:2px 8px;cursor:pointer;font-family:inherit;'
                    f'margin-left:4px;font-size:12px" '
                    f'onclick="trackRen({trk["track_id"]})">✎</button>')
    else:
        trk_html = '<span style="color:var(--muted)">未识别</span>'

    body = f"""
<div class="card">
  <h2>概览</h2>
  <div class="kv">
    <div><span>车型</span><b style="font-size:14px">{_html.escape(stats.get('car_name') or '未识别')}</b></div>
    <div><span>赛道</span><b style="font-size:14px">{trk_html}</b></div>
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
<script>
// 赛道改名：改名写 tracks.json 的 tracks[].name，与场次无关（同赛道所有场次一起变）
window.trackRen = function (id) {{
  var v = prompt('给这条赛道起名（同赛道所有场次一起生效）');
  if (v == null) return;
  v = v.trim();
  if (!v) return;
  fetch('/api/v1/tracks/' + id + '/rename', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{value: v}})
  }}).then(function (r) {{ return r.json(); }}).then(function (d) {{
    if (d.error) {{ alert(d.error); return; }}
    location.reload();
  }}).catch(function () {{ alert('保存失败'); }});
}};
</script>

<div class="card">
  <h2>圈速</h2>
  {f'''<table><tr><th>圈</th><th style="text-align:right">用时</th><th></th></tr>
      {''.join(lap_rows)}</table>''' if lap_rows else
     '<p style="font-size:13px;color:var(--muted)">未能识别出完整的圈（可能只跑了几秒）</p>'}
</div>"""
    # 文件名要嵌进 JS 的单引号字符串里：把反斜杠和单引号剥掉，
    # 免得带奇怪字符的文件名把 __FILE__ 那行拆掉、整段脚本挂掉。
    js_file = path.name.replace("\\", "").replace("'", "")
    # 标题优先用识别出的赛道名（比协议给的 null/unknown 有用得多）
    page_title = (trk.get("name") if trk.get("track_id") is not None
                  and trk.get("name") else None) \
        or str(hdr.get('circuit') or 'unknown')
    return _page_shell(f"场次 · {_html.escape(page_title)}",
                       body + _COMPARE_TMPL.replace('__DATA__', cmp_json)
                       # 四张分析卡排在「遥测数据」之前：先给结论（走线偏哪、胎怎么被
                       # 糟蹋的、能快多少、发生过什么），原始逐帧数据垫底。数据走
                       # XHR 异步取，不占首屏渲染时间。
                       + _DEVIATION_TMPL.replace('__FILE__', js_file)
                       + _ANALYSIS_TMPL.replace('__FILE__', js_file)
                       # 事件时间线在遥测卡之前：它的「点事件 → 十字光标」联动
                       # 要求 _TELEMETRY_TMPL 的 telePinAt 在点击时已可用——
                       # 两个 IIFE 都是 DOMContentLoaded 前定义，顺序只影响
                       # 卡片视觉位置，不影响函数可用性。
                       + _EVENTS_TMPL.replace('__FILE__', js_file)
                       # 录像对齐紧跟事件卡：它展示的正是「事件在录像里第几秒」，
                       # 两张卡一起看才说得通（先看发生了什么，再看去哪儿切）。
                       + _VIDEO_TMPL.replace('__FILE__', js_file)
                       + _PIT_TMPL.replace('__FILE__', js_file)
                       + _TELEMETRY_TMPL.replace('__FILE__', js_file))


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script>/*THEME_BOOT_JS*/</script>
<script>/*ZOOM_JS*/</script>
<title>GT7 遥测仪表盘</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
/*THEME_CSS*/
/*ZOOM_CSS*/
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
/* ---------- 赛道工程师卡片 ---------- */
#c-coach h2 { display:flex; align-items:center; gap:8px; }
#c-coach h2 .cspacer { flex:1; }
#coachDot { width:7px; height:7px; border-radius:50%; background:var(--muted);
  flex-shrink:0; margin:0; }
#coachDot.on { background:var(--ok); box-shadow:0 0 0 3px rgba(63,185,80,.18); }
#coachDot.off { background:var(--warn); }
/* #C 复审（用户反馈"不知道要手动点开"）：关闭态改成**实心橙红 + 呼吸闪烁**
   ——一眼就知道"这个按钮在等我一脚"。点开后回落低调描边态，不再闪。 */
#coachMute { border:1px solid var(--warn); background:var(--warn);
  color:#fff; border-radius:6px; padding:4px 10px; cursor:pointer;
  font-family:inherit; font-size:12px; letter-spacing:.3px;
  text-transform:none; font-weight:700;
  animation:coachMuteHint 1.6s ease-in-out infinite; }
#coachMute.on { border-color:rgba(var(--accent-rgb),.5); background:transparent;
  color:var(--accent); font-weight:500; animation:none; }
@keyframes coachMuteHint { 50% { opacity:.55; } }
/* 播报内容面板：与 #coachMute（全局"要不要出声"）分工不同 —— 它管
   "**哪些内容**出声"。两者是「与」关系：语音关着时，勾选多少都不会出声。 */
#coachPanelBtn { border:1px solid var(--line); background:transparent;
  color:var(--muted); border-radius:6px; padding:2px 8px; cursor:pointer;
  font-family:inherit; font-size:11px; letter-spacing:.3px;
  text-transform:none; font-weight:400; }
#coachPanelBtn:hover { color:var(--text); }
#coachPanelBtn.on { border-color:rgba(var(--accent-rgb),.5); color:var(--accent); }
#coPanel { margin-top:9px; border-top:1px solid var(--line); padding-top:6px; }
#coPanel .cp-row { display:flex; align-items:center; gap:8px; font-size:12.5px;
  padding:4px 0; }
#coPanel .cp-row input { width:14px; height:14px; flex-shrink:0; cursor:pointer;
  accent-color:var(--accent); margin:0; }
#coPanel .cp-row label { flex:1; cursor:pointer; }
#coPanel .cp-row.off label { color:var(--muted); }
#coPanel .cp-desc { color:var(--muted); font-size:11px; }
/* #G：分组下的细分开关行 —— 缩进+小一号，视觉上明确"从属于上一行分组" */
#coPanel .cp-sub { display:flex; align-items:center; gap:7px; font-size:11.5px;
  padding:2px 0 2px 21px; color:var(--muted); }
#coPanel .cp-sub input { width:12px; height:12px; flex-shrink:0; cursor:pointer;
  accent-color:var(--accent); margin:0; }
#coPanel .cp-sub label { flex:1; cursor:pointer; }
#coPanel .cp-sub.off label { color:var(--muted); opacity:.55;
  text-decoration:line-through; }
#coPanel .cp-n { font-family:var(--mono); font-size:11px; color:var(--muted);
  min-width:18px; text-align:right; }
#coPanel .cp-hint { color:var(--muted); font-size:11px; margin-top:6px;
  line-height:1.5; }
/* 云措辞模型：与上面那组开关同一个抽屉，但节标题分开 —— 上面管「说哪些」，
   这里管「用哪个模型说」。分节是为了不让人以为它也是播报开关。 */
#coPanel .cp-sec { font-size:10.5px; letter-spacing:.6px; color:var(--accent);
  margin:12px 0 5px; padding-top:10px; border-top:1px dashed var(--line); }
#coPanel .cm-row { display:flex; gap:6px; margin-top:6px; }
#coPanel .cm-row input { flex:1; min-width:0; background:var(--bg);
  border:1px solid var(--line); border-radius:6px; color:var(--text);
  font-family:var(--mono); font-size:11.5px; padding:4px 7px; }
#coPanel .cm-row input::placeholder { color:var(--muted); }
#coPanel .cm-row input:focus { outline:none;
  border-color:rgba(var(--accent-rgb),.5); }
#coPanel .cm-row button { flex-shrink:0; border:1px solid var(--line);
  background:transparent; color:var(--text); border-radius:6px;
  padding:4px 10px; cursor:pointer; font-family:inherit; font-size:11.5px; }
#coPanel .cm-row button:hover { border-color:rgba(var(--accent-rgb),.5);
  color:var(--accent); }
/* 「未填模型名但云已启用」的提示必须显眼 */
#coPanel .cp-hint.warn { color:var(--warn); }
.coach-row { display:flex; justify-content:space-between; font-size:13px;
  padding:5px 0; border-bottom:1px solid var(--line); }
.coach-row:last-of-type { border-bottom:none; }
.coach-row span { color:var(--muted); }
.coach-row b { font-family:var(--mono); font-weight:500; }
/* delta：负 = 比参考快（绿），正 = 丢时间（红）。与圈速表的时间差同色。 */
.coach-row b.neg { color:var(--ok); }
.coach-row b.pos { color:var(--bad); }
#coSay { margin-top:9px; padding:9px 11px; border-radius:8px;
  background:rgba(var(--accent-rgb),.09);
  border:1px solid rgba(var(--accent-rgb),.22);
  font-size:15px; font-weight:600; line-height:1.4;
  min-height:38px; display:flex; align-items:center; }
#coSay.idle { background:transparent; border-style:dashed;
  border-color:var(--line); color:var(--muted); font-weight:400; font-size:12px; }
#coHist { margin-top:8px; max-height:116px; overflow-y:auto; }
#coHist div { font-size:12px; color:var(--muted); padding:3px 0;
  border-bottom:1px dashed var(--line); }
#coHist div:last-child { border-bottom:none; }
#coHist i { font-style:normal; font-family:var(--mono); font-size:10px;
  color:var(--accent); margin-right:5px; }
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
    <div class="card span2" id="c-coach">
      <h2>赛道工程师
        <span id="coachDot" title="连接状态"></span>
        <span class="cspacer"></span>
        <button id="coachPanelBtn" title="播报内容开关 + 云措辞模型（用哪个模型润色）">播报设置</button>
        <button id="coachHistBtn" title="展开/收起播报历史（默认收起：历史会撑高卡片，把下面的「圈速与油量」顶出屏幕）">历史</button>
        <button id="coachMute" title="点击开启语音播报（浏览器要求先有一次点击）">🔇 点我开语音</button>
      </h2>
      <div class="coach-row"><span>参考圈</span><b id="coRef">--</b></div>
      <div class="coach-row"><span>本圈位置</span><b id="coS">--</b></div>
      <div class="coach-row"><span>对比参考圈</span><b id="coDelta">--</b></div>
      <div class="coach-row"><span>下一个刹车点</span><b id="coBrake">--</b></div>
      <div id="coSay" class="idle">赛道工程师未启动</div>
      <div id="coPanel" hidden></div>
      <div id="coHist" hidden></div>
    </div>

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
      <div id="pitWindow" style="margin-top:5px;font-size:12.5px;
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
    <h2>行车轨迹
      <span style="float:right;display:flex;gap:8px;align-items:center;text-transform:none">
        <span id="mapinfo" style="font-weight:400;color:var(--muted)"></span>
        <select id="mapLapSel" onchange="setMapLap(this.value)"
          style="font-weight:400;font-size:12px;padding:2px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:inherit;font-family:inherit"></select>
        <select id="mapModeSel" onchange="setMapMode(this.value)"
          style="font-weight:400;font-size:12px;padding:2px 6px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:inherit;font-family:inherit">
          <option value="g">按 G 力着色</option>
          <option value="pedal">按踏板着色（赛车线）</option>
        </select>
      </span>
    </h2>
    <canvas id="map" width="760" height="420"
      style="width:100%;max-width:760px;display:block;margin:0 auto"></canvas>
    <div class="legend" id="mapLegend" style="justify-content:center;margin-top:6px"></div>
    <p id="mapHint" style="font-size:11.5px;color:var(--muted);margin-top:8px"></p>
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
  // 行车轨迹的着色模式：'g' = 按 G 力 / 'pedal' = 按踏板（赛车线）。
  // 存在偏好里，刷新页面不会跳回默认模式。
  mapMode: 'g',
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
  if (['g','pedal'].indexOf(p.mapMode) < 0) p.mapMode = 'g';
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
  const ms = $('mapModeSel'); if (ms) ms.value = prefs.mapMode || 'g';
  refreshMapLegend();             // 图例/说明跟着着色模式换
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

// 圈速兜底：GT7 不报 last_lap 时，用「本圈起点时刻」跳变反推上一圈用时。
let lapFbPrevStart = 0, lapFbList = [], lapFbSession = 0;
function render(s) {
  const L = s.latest;
  if (!L) return;

  // 状态栏
  // 🔴 connected 由服务端按「状态文件 5 秒内有无更新」判定，
  //    PS5 关机 / 退游戏 / 游戏未进赛道（不发包）都会让旧文件过期，
  //    必须靠服务端判过期。
  const dot = $('dot');
  if (s.connected) { dot.className = 'dot on'; $('status').textContent = '采集中'; }
  else { dot.className = 'dot off'; $('status').textContent = '已断开（游戏未进入赛道）'; }
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

  // —— 进站窗口（纯油量口径）——
  // 🔴 只由「油量 ÷ 均耗」给出最晚进站圈。轮胎磨损广播协议里没有
  //    （296 字节包无 wear 字段），**不给假轮胎窗口**——轮胎寿命请看
  //    游戏 HUD 自行判断。油车 canStrategy 成立才显示；电车不加油，
  //    电量是否够跑完已在上面的策略行里，这里留空。
  const pw = $('pitWindow');
  if (pw) {
    if (canStrategy && !isEV && avg > 0 && typeof L.gas_level === 'number') {
      const lapsLeft = L.gas_level / avg;          // 还能跑几圈（含小数）
      const full = Math.floor(lapsLeft);           // 能完整跑完的圈数
      let txt, col;
      if (L.laps_in_race > 0 && L.lap + lapsLeft >= L.laps_in_race) {
        txt = '✅ 油量足够跑完剩余 ' + (L.laps_in_race - L.lap + 1) + ' 圈，无需进站';
        col = 'var(--ok)';
      } else if (full <= 0) {
        txt = '🛑 油量撑不完一圈 → 立即进站！';
        col = 'var(--bad)';
      } else {
        // 最晚进站圈 = 当前圈 + 可跑圈数 − 1：留一圈跑进站圈本身，
        // 且保证出站后的油能撑到比赛结束（油够时上面已判「无需进站」）。
        txt = '🛞 油量可跑 ' + full + ' 圈 · 最晚第 ' + (L.lap + full - 1)
          + ' 圈前进站';
        col = full <= 1 ? 'var(--bad)' : full <= 3 ? 'var(--warn)' : 'inherit';
      }
      pw.innerHTML = '进站窗口：' + txt;
      pw.style.color = col;
    } else {
      pw.textContent = '';
    }
  }

  // —— 每圈圈速列表（新圈在上，最快圈标绿★）——
  // 🔴 优先用接收器攒的 lap_times；本场没收到（GT7 不报 last_lap 的场景），
  //    改用量产「本圈起点时刻」的跳变反推上一圈用时，列表不空白。
  let lt = s.lap_times || [];
  if (!lt.length && s.lap_started_at && s.lap_time_source === 'lap' && L.lap > 1) {
    if (lapFbSession !== s.session_start) { lapFbPrevStart = 0; lapFbList = []; lapFbSession = s.session_start; }
    if (lapFbPrevStart && s.lap_started_at !== lapFbPrevStart) {
      const durMs = Math.round((s.lap_started_at - lapFbPrevStart) * 1000);
      // 用兜底列表长度顺次编号（1,2,3…），不依赖 L.lap 时序，避免差一帧错号
      if (durMs > 20000 && durMs < 600000) lapFbList.push([lapFbList.length + 1, durMs]);
    }
    lapFbPrevStart = s.lap_started_at;
    lt = lapFbList;
  }
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
  mapLastPath = s.path || [];
  renderMapLapOptions(s.lap_times);
  drawMap(mapLastPath);
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

// ---------- 踏板开度 → 颜色（与场次详情页的赛车线同一套口径）----------
// 刹车「粉→红」（踩得越重越红），油门「青→绿」（踩得越深越绿）。
// 🔴 与场次详情页 _COMPARE_TMPL 里的 PINK/RED/CYAN/GREEN 必须保持一致，
//    否则同一辆车在实时页和历史页会是两种配色。
var PEDAL_PINK = [255, 105, 180], PEDAL_RED = [255, 40, 40];
var PEDAL_CYAN = [0, 188, 212], PEDAL_GREEN = [0, 200, 83];
function pedalMix(c1, c2, k) {
  k = k < 0 ? 0 : (k > 1 ? 1 : k);
  return 'rgb(' + Math.round(c1[0] + (c2[0] - c1[0]) * k) + ',' +
    Math.round(c1[1] + (c2[1] - c1[1]) * k) + ',' +
    Math.round(c1[2] + (c2[2] - c1[2]) * k) + ')';
}
// 一段折线的颜色：刹车优先（trail braking 视觉上按刹车画）
function pedalColor(b, t, coastCol) {
  b = +b || 0; t = +t || 0;
  if (b > 0.03 && b >= t) return pedalMix(PEDAL_PINK, PEDAL_RED, b);
  if (t > 0.03) return pedalMix(PEDAL_CYAN, PEDAL_GREEN, t);
  return coastCol;
}

// ---------- 行车轨迹 ----------
// 两种着色模式，用户在下拉里自由切换：
//   g      —— 按 G 力大小（蓝→绿→橙→红），看车在哪压榨抓地力
//   pedal  —— 按踏板开度（刹车粉→红 / 油门青→绿），即「赛车线」
// 轨迹点格式：[x, z, gmag, throttle, brake, lap, speed]。
// 🔴 旧场次/旧接收器只有 [x, z, gmag] 三个值，缺失的踏板按 0 处理，
//    此时 pedal 模式等价于整条线都是滑行色——不能因此报错或画不出图。
//
// mapLapNo：0 = 画整场累积轨迹；N = 只画第 N 圈（这就是「行车轨迹」按圈看）。
// 它是每场重来的运行时状态，刻意不写进偏好（换一场圈号就无意义了）。
let mapLapNo = 0;
function mapPoint(p, i) {
  return {
    x: p[0], z: p[1], g: p[2] || 0,
    th: p.length > 3 && p[3] != null ? p[3] : 0,
    bk: p.length > 4 && p[4] != null ? p[4] : 0,
    lap: p.length > 5 ? p[5] : null,
    sp: p.length > 6 ? p[6] : null,
    i: i,
  };
}

function drawMapInto(cv, W, H, rawPath, mode, lapNo) {
  if (!cv) return;
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, W, H);
  const PAD = Math.round(W * 0.026) + 6;
  const info = $('mapinfo');

  // 选圈时只画那一圈（这就是「行车轨迹」的单圈视图）；否则画整场累积轨迹
  let pts = (rawPath || []).map(mapPoint);
  if (lapNo) pts = pts.filter(p => p.lap === lapNo);
  if (pts.length < 2) {
    ctx.fillStyle = gvar('--cv-text', 'rgba(128,128,128,.65)');
    ctx.font = Math.round(H * 0.031) + 'px system-ui,-apple-system,sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(!rawPath || !rawPath.length
      ? '等待赛道数据…（车开动后自动绘制）'
      : (lapNo ? '这一圈还没有轨迹点' : '轨迹点不足'), W / 2, H / 2);
    if (info && lapNo) info.textContent = '';
    return;
  }

  // 包围盒
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
  for (const p of pts) {
    if (p.x < x0) x0 = p.x; if (p.x > x1) x1 = p.x;
    if (p.z < z0) z0 = p.z; if (p.z > z1) z1 = p.z;
  }
  const spanX = Math.max(x1 - x0, 1), spanZ = Math.max(z1 - z0, 1);
  const sc = Math.min((W - 2 * PAD) / spanX, (H - 2 * PAD) / spanZ);
  const ox = (W - spanX * sc) / 2 - x0 * sc;
  const oy = (H - spanZ * sc) / 2 - z0 * sc;
  // ⚠️ 画面 y 轴向下，而 GT7 的 z 也是向下为正；
  //    这里对 z 取反，画出来的方向才和游戏里的小地图一致。
  const px = x => ox + x * sc;
  const py = z => H - (oy + z * sc);

  // 逐段着色
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.lineWidth = Math.max(2.4, W / 320);
  const coastCol = gvar('--accent', '#0d6efd');
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    if (mode === 'pedal') {
      // 用两端均值，避免一个点抖动就换色
      ctx.strokeStyle = pedalColor((a.bk + b.bk) / 2, (a.th + b.th) / 2, coastCol);
    } else {
      ctx.strokeStyle = gColor(Math.max(a.g, b.g));
    }
    ctx.beginPath();
    ctx.moveTo(px(a.x), py(a.z));
    ctx.lineTo(px(b.x), py(b.z));
    ctx.stroke();
  }

  // 起点（绿圈）与当前位置（底色填充 + 强调色描边）
  const s0 = pts[0], sN = pts[pts.length - 1];
  const r0 = Math.max(5, W / 152);
  ctx.fillStyle = gvar('--ok', '#198754');
  ctx.beginPath(); ctx.arc(px(s0.x), py(s0.z), r0, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = gvar('--card', '#fff');
  ctx.strokeStyle = gvar('--accent', '#0d6efd'); ctx.lineWidth = 2.5;
  ctx.beginPath(); ctx.arc(px(sN.x), py(sN.z), r0 * 1.1, 0, Math.PI * 2);
  ctx.fill(); ctx.stroke();

  if (info) {
    let top = 0;
    for (const p of pts) if (p.g > top) top = p.g;
    info.textContent = pts.length + ' 点' +
      (lapNo ? '（第 ' + lapNo + ' 圈）' : '') + ' · 最高 ' + top.toFixed(1) + 'g';
  }
}

function drawMap(path) {
  const cv = $('map'); if (!cv) return;
  drawMapInto(cv, cv.width, cv.height, path, prefs.mapMode || 'g', mapLapNo);
}

// 最近一次收到的轨迹：切换着色模式 / 选圈时立刻重画，不必等下一帧数据
// （状态文件的轨迹是 2Hz 才写一次，等下一帧要半秒，切换会显得「卡住」）。
let mapLastPath = [];

function refreshMapLegend() {
  const lg = $('mapLegend'), hint = $('mapHint');
  if (!lg) return;
  if ((prefs.mapMode || 'g') === 'pedal') {
    lg.innerHTML =
      '<span><i style="width:30px;background:linear-gradient(90deg,#00bcd4,#00c853)"></i>油门（青 → 绿，越深越浓）</span>' +
      '<span><i style="width:30px;background:linear-gradient(90deg,#ff69b4,#ff2828)"></i>刹车（粉 → 红，越重越红）</span>' +
      '<span><i style="background:var(--accent)"></i>滑行</span>';
    if (hint) hint.textContent = '线色来自逐帧记录的踏板开度百分比：刹车踩得越重越红，' +
      '油门踩得越深越绿，两个都不踩的滑行段用强调色。选「整场」看本次全部行驶轨迹；' +
      '选某一圈就得到那一圈的行车轨迹——用来复盘自己每一脚的给油/刹车。';
  } else {
    lg.innerHTML =
      '<span><i style="background:#0d6efd"></i>低 G</span>' +
      '<span><i style="background:#198754"></i>中 G</span>' +
      '<span><i style="background:#fd7e14"></i>高 G</span>' +
      '<span><i style="background:#dc3545"></i>极高 G（重刹/急弯）</span>';
    if (hint) hint.textContent = '线色是车身受到的合力大小：越红说明这一处越接近抓地力极限。' +
      '想改成看油门 / 刹车，切到右边的「按踏板着色」。';
  }
}

function renderMapLapOptions(lt) {
  const sel = $('mapLapSel'); if (!sel) return;
  lt = lt || [];
  const best = lt.length > 1 ? Math.min.apply(null, lt.map(x => x[1])) : null;
  let html = '<option value="0">整场</option>';
  for (let i = lt.length - 1; i >= 0; i--) {     // 新圈排在前面
    const n = lt[i][0], ms = lt[i][1];
    html += '<option value="' + n + '">第 ' + n + ' 圈 · ' + fmtMs(ms) +
      (ms === best ? ' ★最快' : '') + '</option>';
  }
  sel.innerHTML = html;
  // 换场次后原先选中的圈可能已不存在，退回「整场」
  if (mapLapNo && !lt.some(x => x[0] === mapLapNo)) mapLapNo = 0;
  sel.value = String(mapLapNo);
}

function setMapMode(v) {
  prefs.mapMode = (v === 'pedal') ? 'pedal' : 'g';
  savePrefs();
  refreshMapLegend();
  drawMap(mapLastPath);
}

function setMapLap(v) {
  mapLapNo = +v || 0;
  drawMap(mapLastPath);
}

// ---------- G-G 散点图（抓地力圆）----------
// 保留原样：把整场比赛的 (横向G, 纵向G) 打成散点，
// 用来看这辆车/这条赛道把轮胎用到什么程度（抓地力圆）。
function drawGGInto(cv, W, H, gg) {
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const cx = W / 2, cy = H / 2;
  const GMAX = GG_MAX;                    // 量程跟着个性化面板走
  const R = Math.min(W, H) / 2 - 16;
  const sc = R / GMAX;
  const CV_TEXT = gvar('--cv-text', 'rgba(128,128,128,.65)');
  const FS = Math.max(10, Math.round(W / 68));   // 放大时字号同步放大
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
    const dot = Math.max(1.7, W / 400);
    for (let i = 0; i < n; i++) {
      const lat = gg[i][0], lon = gg[i][1];
      const x = cx + lat * sc;
      const y = cy - lon * sc;
      ctx.globalAlpha = 0.12 + 0.55 * (i / n);
      ctx.fillStyle = gColor(Math.hypot(lat, lon));
      ctx.beginPath(); ctx.arc(x, y, dot, 0, Math.PI * 2); ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  ctx.fillStyle = CV_TEXT;
  ctx.font = FS + 'px system-ui,-apple-system,sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('加速', cx, cy - R - 5);
  ctx.fillText('刹车', cx, cy + R + FS + 2);
  ctx.textAlign = 'left';
  ctx.fillText('右转', cx + R + 3, cy + 3);
  ctx.textAlign = 'right';
  ctx.fillText('左转', cx - R - 3, cy + 3);
  if (!gg || !gg.length) {
    ctx.fillStyle = CV_TEXT;
    ctx.textAlign = 'center';
    ctx.fillText('过弯后自动生成', cx, cy - 6);
  }
}

let lastGG = [];        // 最近一次 G-G 散点（放大时按新尺寸重画要用）
function drawGG(gg) {
  lastGG = gg || [];
  const cv = $('gg'); if (!cv) return;
  drawGGInto(cv, cv.width, cv.height, lastGG);
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

function drawBallInto(cv, W, H) {
  if (!cv) return;
  const ctx = cv.getContext('2d');
  // 原始设计尺寸 680×620；放大重画时所有半径/线宽/字号按 k 同步放大，
  // 否则放大版会变成「一堆细线 + 蚂蚁字」。
  const k = W / 680;
  const R = Math.min(W, H) / 2 - 22 * k;
  const cx = W / 2, cy = H / 2;
  const sc = R / GG_MAX;
  const th = ggTheme();
  ctx.clearRect(0, 0, W, H);

  // 浅碗底盘
  ctx.fillStyle = th.bowl;
  ctx.beginPath(); ctx.arc(cx, cy, R + 9 * k, 0, Math.PI * 2); ctx.fill();

  // 参考环
  ctx.lineWidth = Math.max(1, k);
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
    ctx.arc(cx + t.x * sc, cy - t.y * sc, 2.4 * k, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.globalAlpha = 1;

  // 小球（带高光，做出立体感）
  const bx = cx + ggBall.x * sc;
  const by = cy - ggBall.y * sc;
  const gmag = Math.hypot(ggBall.x, ggBall.y);
  ctx.shadowColor = th.shadow; ctx.shadowBlur = 12 * k; ctx.shadowOffsetY = 3 * k;
  const grad = ctx.createRadialGradient(bx - 5 * k, by - 6 * k, 1.5 * k, bx, by, 17 * k);
  grad.addColorStop(0, 'rgba(255,255,255,.95)');
  grad.addColorStop(0.45, gColor(gmag));
  grad.addColorStop(1, gColor(gmag));
  ctx.fillStyle = grad;
  ctx.beginPath(); ctx.arc(bx, by, 16 * k, 0, Math.PI * 2); ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.shadowBlur = 0; ctx.shadowOffsetY = 0;

  // 中心→球的连线，强化方向感
  if (gmag > 0.08) {
    ctx.strokeStyle = 'rgba(140,140,140,.55)';
    ctx.lineWidth = 1.5 * k;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(bx, by); ctx.stroke();
  }

  // 方位标签
  ctx.fillStyle = th.text;
  ctx.font = Math.max(10, Math.round(10 * k)) + 'px system-ui,-apple-system,sans-serif';
  ctx.textAlign = 'center';
  ctx.fillText('加速', cx, cy - R - 10 * k);
  ctx.fillText('刹车', cx, cy + R + 18 * k);
  ctx.textAlign = 'left';
  ctx.fillText('右转', cx + R + 5 * k, cy + 3);
  ctx.textAlign = 'right';
  ctx.fillText('左转', cx - R - 5 * k, cy + 3);

  if (!ggReady) {
    ctx.fillStyle = th.text;
    ctx.textAlign = 'center';
    ctx.fillText('等待车辆数据…', cx, cy - 4);
  }
}

function drawBall() {
  const cv = $('gball'); if (!cv) return;
  drawBallInto(cv, cv.width, cv.height);
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
let _noRespStreak = 0;
function poll() {
  fetch('/api/state?frames=' + prefs.chartWindow)
    .then(r => { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    // 🔴 渲染异常 ≠ 服务断流：单独 try 吞掉，绝不因此误报「服务无响应」——
    //    否则主数据明明在动、状态栏却红着「服务无响应」，车手会以为坏了。
    .then(d => { _noRespStreak = 0;
      try { render(d); } catch (e) { console.error('render 异常（非服务断流）:', e); } })
    // 🔴 偶发抖动（一次 fetch 失败 / JSON 解析卡顿 / 服务在算重活短时无响应）
    //    不该立刻判死。#5 复审：3 次（300ms）太敏感 —— 用户实测"数据明明在动，
    //    状态栏却闪『服务无响应』"。提到 10 次（~1s）：真断了 1 秒内必红，
    //    假抖动基本滤净。
    .catch(() => {
      _noRespStreak++;
      if (_noRespStreak >= 10) {
        $('dot').className = 'dot off';
        $('status').textContent = '服务无响应';
      }
    });
}
poll();
setInterval(poll, 100);   // 10Hz 轮询；G 力球靠 rAF 在两次轮询之间平滑插值
ggLoop();                 // 启动 G 力球动画（自带 requestAnimationFrame 循环）

// ---------- 赛道工程师（gt7-coach）：状态卡片 + 语音播报 ----------
// 跑在**另一个进程**，跨源取数靠对方给 CORS 头。它挂了/没起/地址不对，
// 都只能影响这一张卡 —— 绝不能让主仪表盘跟着出问题。所以全是双参数 then + 退避。
//
// 🔴 地址默认不能是「本页主机 + 8788」：常见部署是仪表盘在服务器
//    （比如 192.168.43.18:8787），而赛道工程师要跑在**玩家电脑**上
//    （扬声器在旁边）。所以按 ?coach= → localStorage → 同主机 :8788 依次取，
//    并且允许点一下卡片上的提示就地改地址、记住。
const COACH_KEY = 'gt7_coach_url';
const COACH_POLL_MS = 200;
let coachUrl = (function(){
  const q = new URLSearchParams(location.search).get('coach');
  if (q) return q.replace(/\/+$/, '');
  try { const v = localStorage.getItem(COACH_KEY); if (v) return v.replace(/\/+$/, ''); }
  catch (e) { /* 隐私模式下 localStorage 会抛，忽略 */ }
  if (!location.hostname) return 'http://127.0.0.1:8788';
  return location.protocol + '//' + location.hostname + ':8788';
})();
// 默认不发声：① 浏览器要求先有一次用户手势才允许自动播放；
//              ② 别在用户没准备的时候突然出声。点一下按钮即开启。
let coachSpeakOn = false;
let coachRetry = COACH_POLL_MS;
let coachSig = '';        // 已播报过的内容指纹（去重）

function coachSay(text, priority){
  if (!coachSpeakOn || !text || !window.speechSynthesis) return;
  try {
    // 🔴 P0（出界/打滑/刹车晚了）要能**打断**正在念的闲话。
    //    浏览器 TTS 默认排队：一句 delta 会把随后的"出界"堵在后面，
    //    等念完就晚了。真赛车无线电是抢麦，不是排队。
    if (priority <= 0 && speechSynthesis.speaking) speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = 'zh-CN'; u.rate = 1.15;
    speechSynthesis.speak(u);
  } catch (e) { /* 播报失败绝不影响取数 */ }
}

// ---------- 播报内容面板（分内容开关）----------
// 与 #coachMute 的分工：那个是**全局**「要不要出声」，这个是**分内容**的
// 「哪些内容出声」。两者是「与」关系 —— 语音关着时，勾选多少都不会出声。
//
// 🔴 真值在教练服务端的 `GateConfig.muted`（`/api/v1/coach/panel`）。
//    这里只做两件事：读回来画、改动后 POST 回去。**本地不存一份** ——
//    存一份就会出现"两个客户端各记各的、谁也说服不了谁"。
let coachPanelOpen = false;
let coachPanelGroups = [];   // 服务端返回的最后一版分组（渲染 + 算下一版 muted）

function coachPanelToggle(){
  coachPanelOpen = !coachPanelOpen;
  $('coPanel').hidden = !coachPanelOpen;
  $('coachPanelBtn').className = coachPanelOpen ? 'on' : '';
  if (coachPanelOpen) loadCoachPanel(false);
}

// 播报历史（#coHist）默认**收起**。
// 🔴 它最长 116px，一旦展开就会把教练卡撑高 ~117px，进而把下面的「圈速与油量」
//    整张卡往下推 —— 视口 ~850~900px 时，圈速**逐圈列表**正好被顶到可视区之外，
//    用户看到的就是「教练一连接，圈速记录就不显示了；断开又回来了」（浏览器实测
//    复现）。默认收起后，教练卡的连接态/未连接态高度一致，**不产生任何位移**；
//    想看历史点一下「历史」按钮即可。
let coachHistOpen = false;
function coachHistToggle(){
  coachHistOpen = !coachHistOpen;
  $('coHist').hidden = !coachHistOpen;
  $('coachHistBtn').className = coachHistOpen ? 'on' : '';
}

// silent=true 用于面板开着时的周期刷新：失败就静静等下一次，
// 别因为一次抖动把面板收起来。
function loadCoachPanel(silent){
  fetch(coachUrl + '/api/v1/coach/panel')
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(renderCoachPanel, function(){
      if (silent) return;
      // 老版教练没有这个接口 / 没启动 → 不报错，把面板收起来并说明原因
      coachPanelOpen = false;
      $('coPanel').hidden = true;
      $('coachPanelBtn').className = '';
      $('coachPanelBtn').title = '当前教练不支持播报设置（需要新版教练服务端）';
    });
}

function coachPanelRowHtml(g){
  const on = !g.muted;
  return '<div class="cp-row' + (on ? '' : ' off') + '" data-id="' + g.id + '">'
    + '<input type="checkbox" id="cp-' + g.id + '"' + (on ? ' checked' : '')
    + '><label for="cp-' + g.id + '">' + g.label
    + '<span class="cp-desc"> · ' + (g.desc || '') + '</span></label>'
    + '<span class="cp-n" title="最近播报条数">' + (g.recent || 0) + '</span></div>'
    // #G：分组下的细分开关（id 直接用教练端 RuleConfig 字段名）
    + (g.subs || []).map(function(s){
        return '<div class="cp-sub' + (s.on ? '' : ' off')
          + '" data-sub="' + s.id + '" data-group="' + g.id + '">'
          + '<input type="checkbox" id="cs-' + s.id + '"' + (s.on ? ' checked' : '')
          + '><label for="cs-' + s.id + '">' + (s.label || s.id) + '</label></div>';
      }).join('');
}

// 抽屉骨架。分两节：① 播报内容（说哪些）② 云措辞模型（用哪个模型说）。
// 🔴 2026-10-10：没有任何预设模型 —— 模型名必须玩家自己填（必填），
//    留空 = 云措辞不可用（服务端回落本地模板）。
function coachPanelShellHtml(rows){
  return '<div class="cp-sec">播报内容</div>'
    + '<div id="coGroups">' + rows + '</div>'
    + '<div class="cp-hint">勾选 = 播报，取消 = 不说。'
    + '「安全告警」不建议关闭。改动立即生效。</div>'
    + '<div class="cp-sec">云措辞模型（#H）</div>'
    + '<div class="cm-row">'
    + '<select id="coCloudPreset" title="选服务商一键填入三框，填完点保存；仍可手改任意一框">'
    + '<option value="">— 服务商预设（一键填入）—</option>'
    + '</select></div>'
    + '<div class="cm-row">'
    + '<input id="coBaseUrlInput" type="text" spellcheck="false" '
    + 'placeholder="base_url（OpenAI 兼容端点）">'
    + '</div>'
    + '<div class="cm-row">'
    + '<input id="coKeyEnvInput" type="text" spellcheck="false" '
    + 'placeholder="API Key 环境变量名（不是 key 本体！）">'
    + '</div>'
    + '<div class="cm-row">'
    + '<input id="coModelInput" type="text" spellcheck="false" '
    + 'placeholder="模型名（必填，自行填写；无任何预设）">'
    + '<button id="coModelSave" title="写入 cloud.json，立即生效">保存</button>'
    + '</div>'
    + '<div class="cp-hint" id="coModelMsg">加载中…</div>'
    + '<div class="cp-sec">打滑灵敏度（#J）</div>'
    + '<div class="cm-row">'
    + '<select id="coSlipPreset" title="三档预设：街道要严、赛道日适中、漂移/拉力/泥地故意滑">'
    + '<option value="strict">严格（街道）</option>'
    + '<option value="standard">标准（赛道日）</option>'
    + '<option value="lenient">宽容（漂移/拉力/泥地）</option>'
    + '</select></div>'
    + '<div class="cm-row" style="align-items:center;gap:10px">'
    + '<input type="range" id="coSlipSlider" min="0.02" max="0.50" step="0.01"'
    + '  style="flex:1;accent-color:var(--accent)">'
    + '<b id="coSlipVal" style="min-width:48px;text-align:right;'
    + 'font-family:var(--mono)">--</b></div>'
    + '<div class="cp-hint" id="coSlipMsg">加载中…</div>';
}

// 🔴 已经渲染过就**只就地改**，不重写 innerHTML —— 重写会把用户正按着的
//    复选框整个换成新的，点击看起来像"没反应"。骨架只在首次建一次。
function renderCoachPanel(d){
  coachPanelGroups = d.groups || [];
  const box = $('coPanel');
  if (!box.querySelector('.cp-row')) {
    box.innerHTML = coachPanelShellHtml(coachPanelGroups.map(coachPanelRowHtml).join(''));
    loadCoachCloud(false);
    loadCoachSlip(false);
    return;
  }
  coachPanelGroups.forEach(function(g){
    const row = box.querySelector('.cp-row[data-id="' + g.id + '"]');
    if (!row) return;
    row.className = 'cp-row' + (g.muted ? ' off' : '');
    const cb = row.querySelector('input');
    if (cb && cb.checked === g.muted) cb.checked = !g.muted;
    const n = row.querySelector('.cp-n');
    if (n) n.textContent = String(g.recent || 0);
    // #G：细分开关就地更新（同样只在状态真变了才动，避免抢用户刚点的）
    (g.subs || []).forEach(function(s){
      const sub = box.querySelector('.cp-sub[data-sub="' + s.id + '"]');
      if (!sub) return;
      sub.className = 'cp-sub' + (s.on ? '' : ' off');
      const scb = sub.querySelector('input');
      if (scb && scb.checked !== s.on) scb.checked = s.on;
    });
  });
}

// #G：切一个细分开关。乐观更新（勾选立刻跟手），POST 到 /config 的 rules
// 节（布尔白名单）—— 子开关的真值在教练服务端 RuleConfig，不在本地。
function saveCoachSub(id, on){
  const patch = {};
  patch[id] = on;
  coachPanelGroups.forEach(function(g){
    (g.subs || []).forEach(function(s){ if (s.id === id) s.on = on; });
  });
  renderCoachPanel({groups: coachPanelGroups});
  fetch(coachUrl + '/api/v1/coach/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rules: patch}),
  })
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }, function(){ throw new Error('net'); })
    // ⚠️ 双参数 then（与 setCoachMuted 同一个坑）：失败时回读服务端，
    //    把乐观更新撤回去；成功时服务端回的 config 本来就是真值。
    .then(function(d){
      const rules = (d.config && d.config.rules) || {};
      coachPanelGroups.forEach(function(g){
        (g.subs || []).forEach(function(s){
          if (s.id in rules) s.on = !!rules[s.id];
        });
      });
      renderCoachPanel({groups: coachPanelGroups});
    }, function(){ loadCoachPanel(true); });
}

// ---------- 云措辞模型（玩家自己填模型名）----------
// 🔴 真值在教练服务端的 cloud.json（`/api/v1/coach/cloud`）。这里只负责
//    「显示现在用的是哪个」+「把玩家填的名字 POST 回去」，**本地不存**。
//
// 2026-10-10：**没有任何预设模型，也不显示任何「免费额度」标注** ——
// 免费承诺会被时间打脸（今天免费、明天可能收费或下架）。模型名玩家
// 自己填，计费情况自己在厂商控制台确认。
let coachCloud = null;      // 服务端返回的最后一版云状态

function loadCoachCloud(silent){
  fetch(coachUrl + '/api/v1/coach/cloud')
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(renderCoachCloud, function(){
      if (silent) return;
      const msg = $('coModelMsg');
      // 老版教练没有这个接口 → 明说，别给用户一个填了没反应的框
      if (msg) { msg.textContent = '当前教练不支持查看云模型（需要新版教练服务端）'; }
      const inp = $('coModelInput');
      if (inp) inp.disabled = true;
      const bu = $('coBaseUrlInput'), ke = $('coKeyEnvInput');
      if (bu) bu.disabled = true;
      if (ke) ke.disabled = true;
    });
}

function renderCoachCloud(d){
  coachCloud = d || {};
  const msg = $('coModelMsg'), inp = $('coModelInput');
  const bu = $('coBaseUrlInput'), ke = $('coKeyEnvInput');
  if (!msg || !inp) return;
  inp.disabled = false; if (bu) bu.disabled = false; if (ke) ke.disabled = false;
  // 🔴 只在输入框**没被编辑**时才回填 —— 面板开着时每 ~3s 刷一次，
  //    无条件回填会把用户正打的字冲掉。
  if (document.activeElement !== inp) {
    inp.value = coachCloud.model || '';
  }
  // #H：base_url / api_key_env 同样只在没被编辑时回填。
  //     key 框回填的是**环境变量名**（明文 key 从不回传 —— 服务端根本没有）。
  if (bu && document.activeElement !== bu) bu.value = coachCloud.base_url || '';
  if (ke && document.activeElement !== ke) ke.value = coachCloud.api_key_env || '';
  // 🔴 2026-10-10：只显示玩家填的模型名本身；不做任何免费/预设置信度
  //    标注。没填就明说云措辞不可用。
  let txt = coachCloud.enabled
    ? (coachCloud.model
        ? ('当前：' + coachCloud.model)
        : '当前：未填写模型名，云措辞不可用（请自行填写）')
    : '云措辞未启用（只用本地模板，零外呼）';
  if (coachCloud.has_key === false && coachCloud.enabled) {
    txt += ' · 环境变量 ' + (coachCloud.api_key_env || '?') + ' 未设置，云调用会回落模板';
  }
  msg.textContent = txt;
  msg.className = 'cp-hint' + (coachCloud.enabled && !coachCloud.model ? ' warn' : '');
}

function saveCoachModel(){
  const inp = $('coModelInput'), msg = $('coModelMsg');
  const bu = $('coBaseUrlInput'), ke = $('coKeyEnvInput');
  if (!inp || !msg) return;
  const model = (inp.value || '').trim();
  const base = bu ? (bu.value || '').trim() : '';
  const envName = ke ? (ke.value || '').trim() : '';
  msg.className = 'cp-hint';
  msg.textContent = '保存中…';
  // 🔴 #H 三框一次 POST。key 框填的是**环境变量名**——真 key 待在环境变量里，
  //    服务端对明文 key 会直接 400 拒收，这里不用重复校验。
  const bodyData = {model: model, base_url: base, api_key_env: envName};
  fetch(coachUrl + '/api/v1/coach/cloud', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(bodyData),
  })
    .then(function(r){
      if (r.ok) return r.json();
      // 把服务端的错误原因（如"未配置 cloud.json"）带给用户，别只说"失败"
      return r.json().then(function(e){
        throw new Error(e && e.error ? e.error : ('HTTP ' + r.status));
      }, function(){ throw new Error('HTTP ' + r.status); });
    })
    // ⚠️ 双参数 then：单参数 .catch 会把 renderCoachCloud 里的异常也算成
    //    "保存失败"，于是报一个和真实原因无关的错 —— 与 pollCoach 同一个坑。
    .then(function(d){
      renderCoachCloud(d.cloud);
      // 没填模型名时给一句黄字提醒（云措辞实际不可用）
      if (coachCloud.model) {
        msg.textContent = '已保存：' + coachCloud.model
          + '。下一句播报就按这个模型走。';
      } else {
        msg.className = 'cp-hint warn';
        msg.textContent = '已保存，但**未填写模型名** —— 云措辞不可用，'
          + '请自行填写。';
      }
    }, function(e){
      msg.className = 'cp-hint warn';
      msg.textContent = '保存失败：' + e.message;
    });
}

// ---------- 打滑灵敏度三档（#J）----------
// 🔴 预设只是标签，真正判据用的是 slip_threshold。选预设时服务端把阈值设回
//    该档基线，之后滑块微调改的就是 slip_threshold 本身。这里既不存预设学名、
//    也不存基线值——全以 /api/v1/coach/config 返回为准（单一事实源）。
function loadCoachSlip(silent){
  fetch(coachUrl + '/api/v1/coach/config')
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(renderCoachSlip, function(){
      if (silent) return;
      const m = $('coSlipMsg');
      if (m) m.textContent = '当前教练不支持打滑三档设置（需要新版教练服务端）';
      const s = $('coSlipSlider'); if (s) s.disabled = true;
      const p = $('coSlipPreset'); if (p) p.disabled = true;
    });
}

function renderCoachSlip(d){
  // #H 顺手搭车：/config 里带了 cloud_presets，首次在这里把服务商预设
  //     下拉填上（只填一次，之后不重写 —— 避免冲掉用户正展开的选项）。
  const cp = $('coCloudPreset');
  if (d.cloud_presets) coachCloudPresets = d.cloud_presets;
  if (cp && cp.options.length <= 1 && d.cloud_presets) {
    Object.keys(d.cloud_presets).forEach(function(k){
      const p = d.cloud_presets[k];
      const opt = document.createElement('option');
      opt.value = k;
      opt.textContent = p.label || k;
      cp.appendChild(opt);
    });
  }
  const sel = $('coSlipPreset'), sl = $('coSlipSlider'),
        val = $('coSlipVal'), msg = $('coSlipMsg');
  if (!sel || !sl) return;
  const r = (d.rules || {});
  // 🔴 不覆盖用户正在操作的控件：面板开着每 ~3s 刷一次，无条件回填会把
  //    正拖着的滑块、正选的下拉冲掉（与云模型输入框同样的坑）。
  if (document.activeElement !== sel) sel.value = r.slip_preset || 'standard';
  const thr = (typeof r.slip_threshold === 'number') ? r.slip_threshold
                                                     : parseFloat(sl.value);
  if (document.activeElement !== sl) {
    sl.value = thr;
    if (val) val.textContent = thr.toFixed(2);
  }
  if (msg && document.activeElement !== sl) {
    msg.textContent = '滑移率超过阈值才报"打滑"。严格=任何打滑都报，'
      + '宽容=故意滑也少报。滑块可在档内微调。';
  }
}

function saveCoachSlipPreset(){
  const sel = $('coSlipPreset'), msg = $('coSlipMsg');
  if (!sel || !msg) return;
  const preset = sel.value;
  msg.className = 'cp-hint';
  msg.textContent = '保存中…';
  fetch(coachUrl + '/api/v1/coach/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rules: {slip_preset: preset}}),
  })
    .then(function(r){
      if (r.ok) return r.json();
      return r.json().then(function(e){
        throw new Error(e && e.error ? e.error : ('HTTP ' + r.status));
      }, function(){ throw new Error('HTTP ' + r.status); });
    })
    .then(function(d){
      renderCoachSlip(d.config);
      msg.textContent = '已切换为「' + presetLabel(preset)
        + '」档，阈值已回到该档基线。';
    }, function(e){
      msg.className = 'cp-hint warn';
      msg.textContent = '保存失败：' + e.message;
    });
}

function saveCoachSlipThreshold(){
  const sl = $('coSlipSlider'), val = $('coSlipVal'), msg = $('coSlipMsg');
  if (!sl || !msg) return;
  const thr = parseFloat(sl.value);
  if (val) val.textContent = thr.toFixed(2);
  msg.className = 'cp-hint';
  msg.textContent = '保存中…';
  fetch(coachUrl + '/api/v1/coach/config', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({rules: {slip_threshold: thr}}),
  })
    .then(function(r){
      if (r.ok) return r.json();
      return r.json().then(function(e){
        throw new Error(e && e.error ? e.error : ('HTTP ' + r.status));
      }, function(){ throw new Error('HTTP ' + r.status); });
    })
    .then(function(d){
      renderCoachSlip(d.config);
      msg.textContent = '阈值已微调为 ' + thr.toFixed(2) + '。';
    }, function(e){
      msg.className = 'cp-hint warn';
      msg.textContent = '保存失败：' + e.message;
    });
}

function presetLabel(v){
  return v === 'strict' ? '严格（街道）'
       : v === 'lenient' ? '宽容（漂移/拉力/泥地）'
       : '标准（赛道日）';
}

// #H：选了服务商预设 → 一键填入三框（base_url / key 环境变量名 / 模型名）。
// 只填框**不保存** —— 用户看清三框内容再点保存，避免"手滑选错立刻生效"。
let coachCloudPresets = null;   // renderCoachSlip 从 /config 抓下来存一份给 applyCloudPreset 用
function applyCloudPreset(key){
  const cp = $('coCloudPreset'), bu = $('coBaseUrlInput'),
        ke = $('coKeyEnvInput'), msg = $('coModelMsg');
  if (!cp || !key) return;
  const p = (coachCloudPresets || {})[key];
  if (!p) return;
  if (bu) bu.value = p.base_url || '';
  if (ke) ke.value = p.api_key_env || '';
  // 🔴 模型名**不预设**（2026-10-10 用户要求）：预设只填端点与 key 变量名，
  //    模型名必须玩家自己填 —— 服务端也没有任何默认模型可兜底，
  //    留空保存 = 云措辞不可用（回落本地模板）。
  if (msg) {
    msg.className = 'cp-hint';
    let t = '已填入「' + (p.label || key) + '」的端点与 key 变量名；'
      + '模型名必填，请自行填写（无任何预设模型）。';
    if (key === 'ollama') {
      t += '本地 Ollama 不校验 key，但需设一个非空环境变量（如 GT7_COACH_LLM_KEY=local）作占位。';
    }
    msg.textContent = t;
  }
}


// 面板开着时搭主轮询的顺风车，每 ~3 s 刷一次（只为更新"最近播报条数"）。
// 主轮询 200ms 一次 → 15 次 ≈ 3 s。
const COACH_PANEL_REFRESH_EVERY = 15;
let coachPanelRefreshN = 0;

function coachPanelMaybeRefresh(){
  if (!coachPanelOpen) return;
  coachPanelRefreshN = (coachPanelRefreshN + 1) % COACH_PANEL_REFRESH_EVERY;
  if (coachPanelRefreshN === 0) { loadCoachPanel(true); loadCoachCloud(true); loadCoachSlip(true); }
}

function setCoachMuted(id, muted){
  const next = [];
  coachPanelGroups.forEach(function(g){
    const m = (g.id === id) ? muted : !!g.muted;
    if (m) next.push(g.id);
    g.muted = m;                    // 乐观更新：勾选立刻跟手
  });
  renderCoachPanel({groups: coachPanelGroups});
  fetch(coachUrl + '/api/v1/coach/panel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({muted: next}),
  })
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }, function(){ throw new Error('net'); })
    // ⚠️ 双参数 then：单参数 .catch 会把 renderCoachPanel 里的异常也当成
    //    "发送失败"，于是回读服务端把用户的点击撤销掉，而真正的原因
    //    （渲染 bug）永远看不见 —— 与 pollCoach 同一个坑。
    .then(renderCoachPanel, function(){ loadCoachPanel(true); });
}

function renderCoach(d){
  coachRetry = COACH_POLL_MS;
  $('coachDot').className = 'on';
  const s = d.stats || {};
  $('coRef').textContent = d.ref_ready
    ? ((s.ref_source === 'profile' ? '车载 60Hz' : '自攒 10Hz')
       + ' · 第 ' + d.ref_lap + ' 圈')
    : '建立中…';
  $('coS').textContent = (d.s_m == null) ? '--'
    : (Math.round(d.s_m) + ' / ' + Math.round(d.ref_len_m || 0) + ' m');
  const dv = d.delta_s, el = $('coDelta');
  if (dv == null) { el.textContent = '--'; el.className = ''; }
  else {
    el.textContent = (dv > 0 ? '+' : '') + dv.toFixed(2) + ' s';
    el.className = dv > 0 ? 'pos' : 'neg';   // 正=丢时间 红，负=更快 绿
  }
  $('coBrake').textContent = (d.next_brake_s == null) ? '--'
    : (Math.round(d.next_brake_m) + ' m / ' + d.next_brake_s.toFixed(1) + ' s');

  const say = (d.say && d.say.length) ? d.say[0] : null;
  const box = $('coSay');
  if (say) {
    box.className = ''; box.textContent = say.text;
    // 播报只念**本次新产生**的那一条：/state 是电平接口，它会一直返回
    // 同一条，不去重就会每 200ms 念一遍。
    // 🔴 语音走 say.speech（数字逐位中文，更贴近真实无线电），屏幕仍显示
    //    say.text（保留 54 / 1:32.412 等原样数字便于扫读）；旧版教练没给
    //    speech 时退回 say.text，向后兼容。
    const spoken = say.speech || say.text;
    const sig = say.key + '|' + spoken + '|' + d.lap;
    if (sig !== coachSig) { coachSig = sig; coachSay(spoken, say.priority); }
  } else if (!d.ref_ready) {
    box.className = 'idle';
    box.textContent = d.connected
      ? '正在建立参考圈：跑完一整圈后才开始给建议'
      : '等待遥测数据…';
  } else {
    // 没新话可说时显示最近说过的那句（灰底），而不是留空 ——
    // 留空会让人以为卡片坏了。
    const last = (d.spoken && d.spoken.length) ? d.spoken[0].text : '';
    box.className = 'idle';
    box.textContent = last || '目前没有要提醒的';
  }

  $('coHist').innerHTML = (d.spoken || []).slice(0, 8).map(function(h){
    return '<div><i>' + (h.key || '').split('@')[0] + '</i>' + h.text + '</div>';
  }).join('');
  // 面板开着时顺路刷新"最近播报条数"（关着时这行什么都不做）
  coachPanelMaybeRefresh();
}

function renderCoachOff(){
  $('coachDot').className = 'off';
  $('coRef').textContent = '--';
  $('coS').textContent = '--';
  $('coDelta').textContent = '--'; $('coDelta').className = '';
  $('coBrake').textContent = '--';
  const box = $('coSay');
  box.className = 'idle';
  box.textContent = '赛道工程师未启动（' + coachUrl + '）· 点这里改地址';
  box.style.cursor = 'pointer';
  box.title = '点击填写赛道工程师的地址，例如 http://localhost:8788';
  $('coHist').innerHTML = '';
  // 教练没了 → 播报面板也没得改（真值在服务端），顺手收起来
  if (coachPanelOpen) { coachPanelOpen = false; $('coPanel').hidden = true; }
  $('coachPanelBtn').className = '';
  // 指数退避：没装/没起的时候别每 200ms 打一个空端口
  coachRetry = Math.min(coachRetry * 2, 15000);
}

// 就地改地址：填对了就记住（localStorage），下次打开不用再填。
// 做成"点提示文字"而不是加一个设置项 —— 这张卡的常态是能用，
// 设置入口不该占常驻空间。
function askCoachUrl(){
  const v = prompt('赛道工程师的地址（通常跑在玩家电脑上）：', coachUrl);
  if (!v) return;
  const url = v.trim().replace(/\/+$/, '');
  if (!/^https?:\/\//.test(url)) { alert('要带 http:// 或 https://'); return; }
  coachUrl = url;
  try { localStorage.setItem(COACH_KEY, url); } catch (e) { /* 忽略 */ }
  coachRetry = COACH_POLL_MS;
  $('coSay').style.cursor = '';
  $('coSay').title = '';
  pollCoach();
}

function pollCoach(){
  fetch(coachUrl + '/api/v1/coach/state')
    .then(function(r){
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    // 🔴 双参数 then，不是 .then(ok).catch(bad)。
    //    单参数写法会把 renderCoach 里的任何异常也当成"取不到数"，
    //    于是整张卡静默显示成"未启动" —— 页面正常、卡没了、console 无线索。
    .then(renderCoach, renderCoachOff)
    .then(function(){ setTimeout(pollCoach, coachRetry); },
          function(){ setTimeout(pollCoach, coachRetry); });
}

(function initCoachCard(){
  const btn = $('coachMute');
  if (!window.speechSynthesis) {
    btn.textContent = '语音不可用';
    btn.disabled = true;
  } else {
      btn.onclick = function(){
        coachSpeakOn = !coachSpeakOn;
        btn.textContent = coachSpeakOn ? '🔊 语音：开' : '🔇 点我开语音';
        btn.className = coachSpeakOn ? 'on' : '';
        // 顺便给个即时反馈（也确认音色/音量是通的）
        if (coachSpeakOn) coachSay('语音已开启');
      };
  }
  $('coSay').onclick = askCoachUrl;
  // 播报内容面板：按钮开合 + 勾选改动（事件委托 —— 行是用 innerHTML
  // 生成的，逐行挂监听会在每次刷新后全部失效）
  $('coachPanelBtn').onclick = coachPanelToggle;
  $('coachHistBtn').onclick = coachHistToggle;
  $('coPanel').addEventListener('change', function(ev){
    const cb = ev.target;
    if (!cb || cb.tagName !== 'INPUT') {
      // 下拉框（SELECT）的 change 也走这里：选了打滑预设就保存；
      // 选了云服务商预设就一键填入三框（#H，只填不存）
      if (cb && cb.id === 'coSlipPreset') saveCoachSlipPreset();
      if (cb && cb.id === 'coCloudPreset') applyCloudPreset(cb.value);
      return;
    }
    if (cb.id === 'coSlipSlider') { saveCoachSlipThreshold(); return; }
    // #G：细分开关在 .cp-sub 里（与 .cp-row 平级，不会误入分组分支）
    const sub = cb.closest ? cb.closest('.cp-sub') : null;
    if (sub) { saveCoachSub(sub.getAttribute('data-sub'), cb.checked); return; }
    const row = cb.closest ? cb.closest('.cp-row') : null;
    if (row) setCoachMuted(row.getAttribute('data-id'), !cb.checked);
  });
  // 滑块拖动时实时更新数字（不保存）；松手时 change 才保存
  $('coPanel').addEventListener('input', function(ev){
    if (ev.target && ev.target.id === 'coSlipSlider') {
      const v = $('coSlipVal');
      if (v) v.textContent = parseFloat(ev.target.value).toFixed(2);
    }
  });
  // 云模型输入框：按钮点一下、或在框里回车都能存。
  // 同样走事件委托 —— 抽屉内容是 innerHTML 生成的，逐项挂监听会失效。
  $('coPanel').addEventListener('click', function(ev){
    if (ev.target && ev.target.id === 'coModelSave') saveCoachModel();
  });
  $('coPanel').addEventListener('keydown', function(ev){
    if (ev.key === 'Enter' && ev.target && ev.target.id === 'coModelInput') {
      ev.preventDefault();
      saveCoachModel();
    }
  });
  pollCoach();
})();

// ---------- 布局系统：卡片显隐 / 半宽整行 / 拖拽排序 / 多布局保存 ----------
// 存 localStorage（每个浏览器各一份）——布局是视觉偏好，跟屏幕尺寸相关，
// 本来就应该按设备存；也避免给服务端加写入接口。
const LAYOUT_KEY = 'gt7_layouts_v2';   // v2：默认布局重排过，废弃旧存档
const CARD_TITLES = {
  'c-rpm':'转速与速度', 'c-pedal':'踏板与 G 力', 'c-lap':'圈速与能量',
  'c-gball':'G 力球', 'c-gg':'G-G 图', 'c-map':'行车轨迹',
  'c-chart':'实时曲线', 'c-wheel':'四轮状态',
  'c-engine':'引擎健康', 'c-race':'比赛信息', 'c-coach':'赛道工程师',
};
const DEFAULT_CARDS = [
  {id:'c-coach',span:2, show:true},
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
  //
  // 🔴 必须是**按默认布局的位置插入**，不能 push 到末尾：
  //    新卡追加到末尾 = 升级后它从第 1 张跳到最后一第，用户眼里就是 bug。
  //    （实测：c-coach 原先既不在 DEFAULT_CARDS 也不在 CARD_TITLES 的
  //      显隐列表里 —— 它靠"不在列表里就不被 applyLayout 碰"偶然可见，
  //      代价是用户**永远没法隐藏或拖动它**。）
  layoutState.layouts.forEach(function (l) {
    const present = new Set(l.cards.map(c => c.id));
    DEFAULT_CARDS.forEach(function (d, i) {
      if (present.has(d.id)) return;
      l.cards.splice(Math.min(i, l.cards.length), 0,
                     JSON.parse(JSON.stringify(d)));
      present.add(d.id);
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

// —— 图表点击放大：登记「怎么画」，屏幕上那份与放大版共用同一段代码 ——
// kw/h 是各图的设计比例，放大时按它算目标尺寸（避免拉伸变形）。
registerZoom('map', {
  title: '行车轨迹', kind: 'canvas', ar: 760 / 420,
  draw: function (cv, W, H) { drawMapInto(cv, W, H, mapLastPath, prefs.mapMode || 'g', mapLapNo); }
});
registerZoom('gg', {
  title: 'G-G 图（抓地力圆）', kind: 'canvas', ar: 680 / 620,
  draw: function (cv, W, H) { drawGGInto(cv, W, H, lastGG); }
});
registerZoom('gball', {
  title: 'G 力球', kind: 'canvas', ar: 680 / 620,
  draw: function (cv, W, H) { drawBallInto(cv, W, H); }
});
// 实时曲线是 SVG：克隆 DOM 交给浏览器矢量放大，比重新拼一遍字符串更省事也更清晰
registerZoom('chart', { title: '实时曲线', kind: 'node', src: 'chart' });

makeZoomable($('map'), 'map');
makeZoomable($('gg'), 'gg');
makeZoomable($('gball'), 'gball');
makeZoomable($('chart'), 'chart');
</script>
/*ZOOM_HTML*/
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 服务
# ---------------------------------------------------------------------------

def main() -> int:
    global PAGE, HUB, _GZIP_ENABLED

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
    p.add_argument("--no-gzip", action="store_true",
                   help="关闭响应 gzip（抓包/排障时看明文）")
    args = p.parse_args()

    _GZIP_ENABLED = not args.no_gzip

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