#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GT7 遥测接收服务（容器常驻版）
==============================

在 Linux 服务器上常驻，监听 GT7 广播的 UDP 遥测，按场次落盘为 .jsonl。

设计要点
--------
1. **只用标准库**——容器不需要装一堆依赖，镜像可以做到 ~120MB。
2. **自己解UDP 包**——不依赖 gttelemetry（Go 库）。原因见下方「为什么自己解」。
3. **自动发现 PS5**——UDP 广播是无连接的，无法知道对端是谁。
   用 PS5 的 Hello/Discovery 协议做反向探测。
4. **掉线自动重连**——PS5 关机 / 进菜单 / 换场景都会停广播，
   服务要能一直等，而不是退出。
5. **增量落盘**——写 .jsonl 而不是 .gtz，方便后处理流式读取，
   也不需要实现 Salsa20 加密。

为什么自己解包而不用 gttelemetry
-------------------------------
gttelemetry 是 Go 库，功能全（含 .gtz 回放），但：
  - 需要在容器里装 Go 工具链，构建变重
  - 它是为「实时显示」设计的，不做批量落盘
本服务定位是**常驻采集 + 落盘**，用Python 标准库直接解更轻。
代价是不支持 PlayStation 专属加密格式（见「已知限制」）。

已知限制
--------
- **不支持 Salsa20 加密的 GT7 格式**。GT7 有 Addendum2/3 等格式，
  部分格式带 Salsa20 加密。本服务先按明文解，检测到加密会明确报错。
  如果你的 PS5 强制加密，需要改用 zetetos/gt-telemetry 的 Go 版。
  → 见 detect_encryption()，它会在日志里明确告诉你。
- **不解 Addendum2/3 的全部字段**，只解复盘必需的核心字段。
  完整字段解析请用 zetetos/gt-telemetry，然后读本服务落盘的 jsonl。

用法
----
    python gt7-recorder.py --output ./data --ps5 192.168.43.100

    # 全部参数
    python gt7-recorder.py --help
"""

from __future__ import annotations

import argparse
import json
import math
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# GT7 协议常量
# ---------------------------------------------------------------------------

# 明文包的 magic number（**不是**加密特征——踩过这个坑）
# 🔴 magic 的字节序坑（踩过一次）
#
# GT7 的 magic 数值是 0x47375330，但**包头里是小端存放**，
# 所以实际字节是 b"0S7G"，**不是** b"GT7\x00"！
#   struct.pack("<I", 0x47375330) == b'0S7G'
#
# 我原先写成 b"GT7\\x00"（想当然按ASCII 读），导致：
#   · decode_many 判定「是明文吗」时永远为假
#   · 所有**已解密**的包又被送进解密器 → 二次解密失败 → 返回 None
#   · 症状：jsonl 有数据（走单包路径）但状态文件 latest 全 0
PLAIN_MAGIC = b"0S7G"
PLAIN_MAGIC_U32 = 0x47375330

# 端口
PORT_HEARTBEAT = 33739# 格式 A：心跳 + 轮速/转速
PORT_ADVANCED = 33740        # 格式 B/C：Addendum 扩展字段

# 会话状态机
IDLE, SESSION_STARTED, SESSION_ACTIVE = "IDLE", "SESSION_STARTED", "SESSION_ACTIVE"

# 动力类型（powertrain）：由 gas_capacity 判定。
#
# 🔴 官方字段说明：fuelCapacity 的范围是
#      100（多数燃油车）→ 5（卡丁车）→ 0（纯电车）
#    社区的 getPowertrainType() 就是照这个实现的：
#      fuel_capacity == 0 → 电动；== 5 → 卡丁车；> 0 → 燃油
#
# ⚠️ 判据有边界：GT7 里燃油车容量被归一化成「100%」，并不披露真实升数；
#    卡丁车恰好是 5。所以不能直接说「小于 10 就是卡丁车」——
#    这里只严格照 == 0 / == 5 判，其余一律算燃油（含混动，GT7 不对
#    混动单独标记，混动车的容量仍 > 0，格式 A 里也没有电池字段）。
POWERTRAIN_FUEL, POWERTRAIN_ELECTRIC, POWERTRAIN_KART = "fuel", "electric", "kart"


def classify_powertrain(gas_capacity: float) -> str:
    """按油箱容量判定动力类型。容差用 1e-3，避免浮点误差把卡丁车判成燃油。"""
    if gas_capacity <= 1e-3:
        return POWERTRAIN_ELECTRIC
    if abs(gas_capacity - 5.0) <= 1e-3:
        return POWERTRAIN_KART
    return POWERTRAIN_FUEL


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """一次遥测采集会话（一次进计时赛 = 一个会话）。"""
    session_id: str
    started_at: float
    circuit: str | None = None
    car: str | None = None
    player_index: int | None = None
    sample_count: int = 0
    last_sample_t: float = 0.0
    laps_seen: set[int] = field(default_factory=set)
    max_speed: float = 0.0
    # 本场动力类型（fuel / electric / kart），由首帧的油箱容量判定。
    # 记录在会话级，便于事后分析 jsonl 时知道 gas_level 的单位是升还是 kWh。
    powertrain: str = "fuel"
    # 本场出现过的最大能量回收值（kW 量级），仅扩展包有值时非 0
    max_energy_recovery: float = 0.0
    state: str = SESSION_STARTED
    source_ip: str | None = None
    # True = 已确认车辆在行驶，数据才真正落盘。
    # False = 收到了包但车没动（菜单/停车场/加载中），只更新状态不落盘。
    recording_started: bool = False

    def to_header(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "started_at_iso": datetime.fromtimestamp(
                self.started_at, tz=timezone.utc
            ).isoformat(),
            "circuit": self.circuit,
            "car": self.car,
            # 动力类型：fuel / electric / kart。
            # ⚠️ 电动车时各帧的 gas_level 是**剩余电量 kWh**（不是百分比），
            #    容量字段为 0；分析 jsonl 时要按这个标记决定单位。
            "powertrain": self.powertrain,
        }


@dataclass
class TelemetrySample:
    """
    一帧遥测。字段名与 gt7-event-detector.py 的 Sample 对齐，
    保证后处理模块可以直接消费。
    """
    t: float
    seq: int
    speed_kph: float = 0.0
    rpm: float = 0.0
    gear: int = 0
    car_x: float = 0.0
    car_z: float = 0.0
    lap: int = 0
    throttle: float = 0.0
    brake: float = 0.0
    # 四轮（FL FR RL RR）
    wheel_rads: list[float] = field(default_factory=lambda: [0.0] * 4)
    tyre_temp: list[float] = field(default_factory=lambda: [0.0] * 4)
    tyre_press: list[float] = field(default_factory=lambda: [0.0] * 4)
    tyre_wear: list[float] = field(default_factory=lambda: [0.0] * 4)
    susp_height: list[float] = field(default_factory=lambda: [0.0] * 4)
    g_force: list[float] = field(default_factory=lambda: [0.0] * 3)
    best_lap: float | None = None
    last_lap: float | None = None
    lap_count: int | None = None
    position: int | None = None# 位置：格式 A 有，格式 B/C 可能没有

    # 本帧的布局与能力标记
    layout: str = "A"            # 见 Decoder._decode_plain
    has_coords: bool = False     # 是否含车身坐标（InvoGT 表显示格式 A 就带）

    # —— 以下为 InvoGT 偏移表新增 ——
    car_y: float = 0.0# position[1]，高度
    velocity: list[float] = field(default_factory=lambda: [0.0] * 3)
    wheel_revs: list[float] = field(default_factory=lambda: [0.0] * 4)
    suggested_gear: int = 0             # gear 高 4 位
    flags: int = 0                      # 状态位
    car_on_track: bool = False          # ★ 官方标志：车在赛道上
    paused: bool = False                # 游戏暂停
    loading: bool = False               # 加载/处理中
    in_gear: bool = False
    hand_brake: bool = False
    laps_in_race: int = 0
    best_lap_ms: int | None = None
    last_lap_ms: int | None = None
    # —— 燃油 / 电量 ——
    # 🔴 语义取决于动力类型（powertrain）：
    #   · fuel（燃油/混动）：gas_level = 燃油量，gas_capacity = 油箱容量（GT7 多数车归一为 100）
    #   · electric（纯电）：gas_capacity == 0，**gas_level 变成剩余电量 kWh**
    #   · kart（卡丁车）：gas_capacity == 5
    # 判据来自官方字段说明 + 社区 getPowertrainType() 实现：
    #   RANGE: 100（多数车）→ 5（卡丁车）→ 0（纯电）
    # 所以百分比必须用 gas_level / gas_capacity 算，不能直接拿 gas_level 当百分比
    # （油箱容量不是 100 的车，例如卡丁车 5，直接当百分比会差一个量级）。
    gas_level: float = 0.0
    gas_capacity: float = 0.0
    powertrain: str = "fuel"          # fuel / electric / kart
    # —— 扩展能量字段（仅在心跳用 "~" 扩展包时非 0）——
    # ⚠️ 社区文档标注这些字段「仍在研究中」，偏移可能随固件变化。
    #    默认心跳是 "A"，拿不到这些值（保持 0），需要显式 --packet-type ~。
    energy_recovery: float = 0.0       # 能量回收功率（正 = 回收，负 = 输出）
    throttle_filtered: float = 0.0     # 游戏内部滤波后的油门输出（0~1）
    brake_filtered: float = 0.0        # 游戏内部滤波后的刹车输出（0~1）
    car_code: int = 0
    turbo_boost: float = 0.0
    # —— 引擎健康（格式 A 内，长期没解析）——
    oil_pressure: float = 0.0     # 0x54 油压（bar）
    water_temp: float = 0.0       # 0x58 水温（℃）
    oil_temp: float = 0.0         # 0x5C 油温（℃）
    body_height: float = 0.0      # 0x38 车身高度（米）
    # —— 比赛信息 ——
    time_of_day: int = 0          # 0x80 赛道时钟（毫秒，当天已过时间）
    quali_pos: int = 0            # 0x84 发车位
    num_cars: int = 0             # 0x86 参赛车辆数
    # —— 换挡提示转速（转速条/换挡灯用）——
    min_alert_rpm: float = 0.0    # 0x88
    max_alert_rpm: float = 0.0    # 0x8A

    def to_json(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 4),
            "seq": self.seq,
            "speed_kph": round(self.speed_kph, 2),
            "rpm": round(self.rpm, 1),
            "gear": self.gear,
            "car_x": round(self.car_x, 4),
            "car_z": round(self.car_z, 4),
            "lap": self.lap,
            "throttle": round(self.throttle, 3),
            "brake": round(self.brake, 3),
            "wheel_rads": [round(w, 2) for w in self.wheel_rads],
            "tyre_temp": [round(t, 1) for t in self.tyre_temp],
            "tyre_press": [round(p, 1) for p in self.tyre_press],
            "tyre_wear": [round(w, 1) for w in self.tyre_wear],
            "susp_height": [round(h, 2) for h in self.susp_height],
            "g_force": [round(g, 3) for g in self.g_force],
            "best_lap": self.best_lap,
            "last_lap": self.last_lap,
            "lap_count": self.lap_count,
            "position": self.position,
            "layout": self.layout,
            "has_coords": self.has_coords,
            "car_y": round(self.car_y, 4),
            "velocity": [round(v, 3) for v in self.velocity],
            "wheel_revs": [round(w, 3) for w in self.wheel_revs],
            "suggested_gear": self.suggested_gear,
            "flags": self.flags,
            "car_on_track": self.car_on_track,
            "paused": self.paused,
            "loading": self.loading,
            "in_gear": self.in_gear,
            "hand_brake": self.hand_brake,
            "laps_in_race": self.laps_in_race,
            "best_lap_ms": self.best_lap_ms,
            "last_lap_ms": self.last_lap_ms,
            "gas_level": round(self.gas_level, 2),
            "gas_capacity": round(self.gas_capacity, 2),
            "powertrain": self.powertrain,
            "energy_recovery": round(self.energy_recovery, 3),
            "throttle_filtered": round(self.throttle_filtered, 3),
            "brake_filtered": round(self.brake_filtered, 3),
            "car_code": self.car_code,
            "turbo_boost": round(self.turbo_boost, 3),
            "oil_pressure": round(self.oil_pressure, 2),
            "water_temp": round(self.water_temp, 1),
            "oil_temp": round(self.oil_temp, 1),
            "body_height": round(self.body_height, 4),
            "time_of_day": self.time_of_day,
            "quali_pos": self.quali_pos,
            "num_cars": self.num_cars,
            "min_alert_rpm": round(self.min_alert_rpm, 0),
            "max_alert_rpm": round(self.max_alert_rpm, 0),
        }


# ---------------------------------------------------------------------------
# 解码器
# ---------------------------------------------------------------------------

class Decoder:
    """
    GT7 UDP 包解码器。

    ⚠️ 格式 A 的字段偏移来自社区逆向工程（Nenkai 的文档、
    MacManley/gt7-udp 的 C++ 实现）。GT7 固件更新可能改协议。
    解码异常时服务会记录警告并跳过该包，而不是崩溃。
    """

    # 格式 A 包头
    A_HEADER_FMT = "<4sBBBIi"      # magic, frame, player, packet_id, seq, session

    def __init__(self) -> None:
        self.encrypted_packets_seen = 0
        self.decode_errors = 0
        self.last_warn: str | None = None
        # 统计各布局的帧数，便于判断 PS5 实际在发哪种格式
        self.frames_by_layout: dict[str, int] = {}
        self.last_layout: str | None = None
        # 非遥测包（能解密但内容全 0）计数——PS5 会混发，实测占约 39%
        self.not_telemetry_packets = 0
        # 上一帧的速度矢量与时间戳（用于差分算 G 力）
        self._last_vel: list[float] | None = None
        self._last_t = 0.0
        # G 力低通滤波状态
        self._last_g: list[float] | None = None
        # Salsa20 解密：每次批量调用一次子进程（不是常驻）
        self.decryptor_path = ""
        self._dec_in = ""
        self._args_path_hint = ""

    # -- Salsa20 解密（委托给 Go 实现的子进程）---------------------------

    def _start_decryptor(self, path: str, log: Any = None) -> None:
        """
        检查解密器可用性。

        ⚠️ 架构说明：不是常驻进程，而是**每次批量调用一次** `subprocess.run`。
        最初想用常驻管道，但踩了两个坑：
          1. Go 的 bufio.Scanner 预读 → Python 按行 readline 行数对不上 → 死等
          2. 即使配select 超时，也出现「单独跑正常，走管道 0/5 成功」
        文件 + subprocess.run 是最可靠的方案。
        60Hz 下每轮 fork 一次（约几 ms），开销可接受。

        通信格式：hex 文本，一行一个包。
        - stdin  原始包（hex）
        - stdout 明文（hex）或 FAIL
        """
        import tempfile

        if not path or not Path(path).exists():
            self.decryptor_path = ""
            self.last_warn = (
                f"找不到解密器 {path}。GT7 遥测是 Salsa20 加密的，没有它无法解码。"
            )
            if log:
                log(self.last_warn, True)
            return

        self.decryptor_path = path
        fd, self._dec_in = tempfile.mkstemp(prefix="gt7_dec_in_", suffix=".hex")
        os.close(fd)
        msg = f"已加载 Salsa20 解密器 {path}"
        if log:
            log(msg, True)
        else:
            print(f"[decoder] {msg}", flush=True)

    def _decrypt_batch(self, packets: list[bytes]) -> list[bytes | None]:
        """
        批量解密：**走临时文件**而非管道。

        ⚠️ 为什么不用管道（踩了两次坑）：
        1. Go 侧用 `bufio.Scanner` 读 stdin，它会**预读**，
           可能把多行一次性吞进缓冲区；Python 侧按行readline
           就永远对不上行数 → 阻塞死等（曾导致整个接收器卡死）。
        2. `bufsize=0` 的原始字节流与 Scanner 混用时，
           行边界会错乱，实测出现「解密器单独跑正常，
           走管道 0/5 成功」的诡异现象。

        临时文件方案没有这些问题：Go 侧顺序读、顺序写，
        语义明确，60Hz 下开销完全可忽略（几百 KB 的临时文件）。

        返回与输入等长的列表，元素为明文或 None（解密失败）。
        """
        n = len(packets)
        if n == 0:
            return []

        # 解密器不可用就重新检查一次
        if not self.decryptor_path:
            self._start_decryptor(self._args_path_hint or "")
        if not self.decryptor_path or not self._dec_in:
            return [None] * n

        try:
            # 1. 写输入文件
            with open(self._dec_in, "w", encoding="ascii") as f:
                for p in packets:
                    f.write(p.hex() + "\n")

            # 2. 调一次解密器。用 communicate() 而非管道 + readline：
            #    Go 的 bufio.Scanner 会预读，直接走管道会导致行边界错乱
            #    （实测「单独跑正常，走管道 0/5 成功」）。
            #    communicate() 以文件为 stdin/stdout，语义明确，不会预读问题。
            with open(self._dec_in, "rb") as fin:
                res = subprocess.run(
                    [self.decryptor_path],
                    stdin=fin,
                    capture_output=True,
                    timeout=2.0,
                )
            raw_out = res.stdout
        except subprocess.TimeoutExpired:
            self.last_warn = "解密器超时（2秒）"
            return [None] * n
        except Exception as e:
            self.last_warn = f"解密器调用失败: {e}"
            self._restart_decryptor()
            return [None] * n

        # 3. 解析输出（一行一个明文或 FAIL）
        out: list[bytes | None] = []
        for line in raw_out.split(b"\n"):
            if not line:
                continue
            line = line.strip()
            if not line or line == b"FAIL":
                out.append(None)
            else:
                try:
                    out.append(bytes.fromhex(line.decode()))
                except ValueError:
                    out.append(None)

        fail = sum(1 for x in out if x is None)
        if fail:
            self.encrypted_packets_seen += fail
        while len(out) < n:
            out.append(None)
        return out[:n]

    def _restart_decryptor(self) -> None:
        """重启解密器检查（实际是重新验证路径）。"""
        if self.decryptor_path:
            self._start_decryptor(self.decryptor_path)

    # -- 基础检查 ---------------------------------------------------------

    @staticmethod
    def detect_encryption(data: bytes) -> bool:
        """
        检测是否需要解密。

        🔴 这个判断我改过三次，最终结论：
        **GT7 真机发来的包 100% 是 Salsa20 加密的**，
        解密后偏移 0 才是 magic 0x47375330（"GT7\0"）。

        我最初的假设（明文包 + 有 magic）是错的，导致所有真包被丢弃。
        现在改成：有 magic = 已经是明文；否则走解密器。
        """
        return len(data) >= 4 and data[:4] != PLAIN_MAGIC

    def decode(self, data: bytes, recv_time: float) -> TelemetrySample | None:
        """解一帧。失败返回 None（已记录警告）。"""
        if len(data) < 32:
            return None

        # 已是明文（少数情况）
        if data[:4] == PLAIN_MAGIC:
            return self._decode_plain(data, recv_time)

        # 加密 → 交给解密器
        plain = self._decrypt_batch([data])[0]
        if plain is None:
            self.encrypted_packets_seen += 1
            return None
        return self._decode_plain(plain, recv_time)

    def decode_many(
        self,
        packets: list[bytes],
        times: list[float] | float,
    ) -> list[TelemetrySample | None]:
        """
        批量解码（性能关键路径）。

        60Hz 下每包起一个进程是不可接受的，所以：
        1. 先批量解密（走临时文件，一次调用）
        2. 再逐个解析明文

        🔴 `times` 必须**逐包**给，不能整批共用一个时间戳。
        踩过的坑：原先整批传一个 now，导致同批几十帧时间戳完全相同，
        于是 G 力恒为 0（dt=0 被守卫跳过）、时间轴失真。
        """
        if not packets:
            return []

        # 兼容「单个时间」的旧调用方式
        if isinstance(times, (int, float)):
            tlist: list[float] = [float(times)] * len(packets)
        else:
            tlist = list(times)
            if len(tlist) < len(packets):
                tlist += [tlist[-1] if tlist else time.time()] * (
                    len(packets) - len(tlist)
                )

        # 分出明文和密文
        need_decrypt = [p for p in packets if p[:4] != PLAIN_MAGIC]
        plains = self._decrypt_batch(need_decrypt) if need_decrypt else []

        results: list[TelemetrySample | None] = []
        pi = 0
        for idx, p in enumerate(packets):
            ts = tlist[idx]
            if p[:4] == PLAIN_MAGIC:
                try:
                    results.append(self._decode_plain(p, ts))
                except Exception:
                    results.append(None)
            else:
                plain = plains[pi] if pi < len(plains) else None
                pi += 1
                if plain is None:
                    self.encrypted_packets_seen += 1
                    results.append(None)
                else:
                    try:
                        results.append(self._decode_plain(plain, ts))
                    except Exception:
                        self.decode_errors += 1
                        results.append(None)
        return results

    def _looks_encrypted(self, data: bytes) -> bool:
        """
        加密检测。

        ⚠️ 这里踩过两个坑，都表现为「一直收不到数据」：

        坑1：最初把包长不在 100~400 之间就判定为加密，
             结果把真实的 Addendum2 包（>400 字节）全部误杀。

        坑2：把 `data[:4] == b"GT7\\x00"` 当加密特征，
             但那**是明文包的 magic**，于是所有正常包都被误杀。

        正确做法：只看 magic 在不在。加一个头部合理性启发式。
        """
        # 无明文 magic → 加密（或垃圾包）
        if self.detect_encryption(data):
            return True

        # 最小长度：连包头都不够
        if len(data) < 32:
            return True

        # 头部合理性：player_index 正常 0~15，packet_id 通常远小于 0xFF
        player_index = data[5]
        packet_id = struct.unpack_from("<H", data, 6)[0]
        if player_index > 16 or packet_id > 0xFF:
            return True

        return False

    # -- 明文解码 ---------------------------------------------------------

    def _decode_plain(self, data: bytes, recv_time: float) -> TelemetrySample | None:
        """
        明文包解码 —— 字段偏移表来自 **InvoGT**（商业成熟软件，权威参考）。

        🔴 这段偏移表换了三次才对，前两次都是错的：
        1. 最初套社区「格式 A」的说法（速度@56、档位@116…）→读出「圈 52679」这种离谱值
        2. 又套 Addendum2 的偏移（car_x@300）→ 格式 A 只有 296 字节，越界
        3. 最终从 InvoGT 的 `createTelemetryPacket`（binary-sensor 链式定义）
           反推出准确偏移，**用你的真实包逐字段验证全部通过**

        InvoGT 偏移表（binary-sensor 链式顺序 = 内存顺序，累加得出）：
            0x00 magic(4)     0x04 position[3](12)  0x10 velocity[3](12)
            0x1C rotation[3]   0x28 relNorth(4)      0x2C angVel[3](12)
            0x38 bodyHeight    0x3C engineRPM(4)     0x40 iv(4)
            0x44 gasLevel      0x48 gasCapacity      0x4C metersPerSecond(4) ★速度
            0x50 turboBoost    0x54 oilPressure      0x58 waterTemp
            0x5C oilTemp0x60 tyreTemp_FL     0x64 tyreTemp_FR
            0x68 tyreTemp_RL    0x6C tyreTemp_RR
            0x70 packID(4)     0x74 lapCount(2)      0x76 lapsInRace(2)
            0x78 bestLapTime   0x7C lastLapTime      0x80 timeOfDay
            0x84 qualiPos       0x86 numCars          0x88 minAlertRPM
            0x8A maxAlertRPM    0x8C calcMaxSpeed     0x8E flags(2) ★状态位
            0x90 gear(1)★      0x91 throttle(1)★     0x92 brake(1)★
            0xA4 wheelRev_FL   0xA8 wheelRev_FR0xAC wheelRev_RL
            0xB0 wheelRev_RR    0xB4 tyreRadius_FL   ...
            0x124 gearRatio[8] 0x144... 0x124carCode@0x124
        合计 296 字节 —— 与真实包长度完全一致，这是偏移表正确的重要佐证。

        ★ 关键差异（与我之前的错误假设对比）：
          - 速度是 **float32 @0x4C**（米/秒），不是 uint16*1000
          - 转速是 **float32 @0x3C**，不是 uint16*2
          - 胎温是 **float32**（摄氏度），不是 uint8-50
          - **position 就是车身坐标（x,y,z 三个 float）** —— 不需要 Addendum2！
            这意味着 `gt7-overtake-detector.py` 的赛道定位从第一帧就能用
        """
        n = len(data)
        if n < 0x9A:            # 至少要覆盖到 brake(0x92)+1
            return None

        f32 = lambda o: struct.unpack_from("<f", data, o)[0]
        u16 = lambda o: struct.unpack_from("<H", data, o)[0]
        u32 = lambda o: struct.unpack_from("<I", data, o)[0]
        f3 = lambda o: list(struct.unpack_from("<3f", data, o))

        try:
            magic = u32(0)
            if magic != PLAIN_MAGIC_U32:
                self.not_telemetry_packets += 1
                return None

            position = f3(0x04)
            velocity = f3(0x10)
            engine_rpm = f32(0x3C)
            pack_id = u32(0x70)
            lap_count = u16(0x74)
            laps_in_race = u16(0x76)
            best_lap = u32(0x78)
            last_lap = u32(0x7C)
            flags = u16(0x8E)

            # 🔴 过滤「空壳包」—— 实测发现的关键问题
            #
            # PS5 在 33740 上除了遥测包，还会发**另一种数据包**：
            # 它同样能通过 Salsa20 解密、magic 也是 0x47375330，
            # 但内容几乎全 0（lap=0xFFFF、坐标 0、胎温 0、flags=0）。
            # 实测占比约 39%（抽样 1508 帧里有 576 帧是这种）。
            #
            # 危害：它会让状态文件的 `latest` 变成全 0，
            #       仪表盘就显示「采集中但数值一动不动」——
            #       而 jsonl 里的真实数据其实是对的（速度 134km/h）。
            #       这个 bug 极隐蔽：落盘数据对、界面却不动。
            #
            # 判据：真实遥测包至少要有实质数据。
            #       速度/转速/坐标/flags 全为 0 → 判为非遥测包。
            if (
                f32(0x4C) == 0.0# metersPerSecond
                and engine_rpm == 0.0
                and position == [0.0, 0.0, 0.0]
                and flags == 0
            ):
                self.not_telemetry_packets += 1
                return None

            gear_byte = data[0x90]
            gear = gear_byte & 0x0F          # 低 4 位 = 当前档
            suggested = gear_byte >> 4        # 高 4 位 = 建议档

            throttle = data[0x91] / 255.0
            brake = data[0x92] / 255.0

            tyre_temp = [
                f32(0x60), f32(0x64), f32(0x68), f32(0x6C),
            ] if n >= 0x70 else [0.0] * 4

            wheel_revs = [
                f32(0xA4), f32(0xA8), f32(0xAC), f32(0xB0),
            ] if n >= 0xB4 else [0.0] * 4

            susp = [
                f32(0xD0), f32(0xCC), f32(0xC8), f32(0xC4),
            ] if n >= 0xD4 else [0.0] * 4
            # 注意：InvoGT 顺序是 RR,RL,FR,FL，我改成 FL,FR,RL,RR 统一

            speed_ms = f32(0x4C)
            speed_kph = speed_ms * 3.6

            # G 力：格式 A 没有直接字段，用**速度矢量差分**算
            #
            # 原理：a = Δv / Δt，再按速度方向拆成纵向/横向：
            #   · 纵向 = a 在速度方向上的投影（加速/刹车）
            #   · 横向 = a 垂直于速度方向的分量（过弯离心）
            # 过弯时速度矢量旋转 → Δv 垂直于速度 → 正好体现为横向 G，
            # 所以这个方法能正确反映弯道 G 力。
            #
            # 单位换算：m/s² ÷ 9.81 = g
            g_force = [0.0, 0.0, 0.0]
            if self._last_vel is not None:
                dt = recv_time - self._last_t
                # 间隔要在合理范围：太短噪声大，太长（掉帧/暂停）无意义
                if 0.001 < dt < 0.5:
                    dv = [velocity[i] - self._last_vel[i] for i in range(3)]
                    acc = [x / dt for x in dv]# m/s²
                    v0 = self._last_vel
                    vmag = math.sqrt(sum(x * x for x in v0))
                    if vmag > 1.0:      # 低速时方向不可靠，跳过
                        u = [x / vmag for x in v0]
                        a_long = sum(acc[i] * u[i] for i in range(3))

                        # 横向：必须取**带符号**的值，否则仪表盘的 G 力球
                        # 只会朝一侧偏、不会左右摇。
                        #
                        # 做法：在水平面内取一个垂直于速度方向的基向量
                        #   perp = up × u    (up = (0,1,0))
                        #        = (u_z, 0, -u_x)，再归一化
                        # 把加速度投影到 perp 上就得到有正负的横向分量。
                        # 正负对应「左转/右转」哪一边取决于 GT7 的坐标手性，
                        # 若实测左右反了，把下面这行的符号取反即可。
                        px, pz = u[2], -u[0]
                        pmag = math.hypot(px, pz)
                        if pmag > 1e-6:
                            a_lat = acc[0] * (px / pmag) + acc[2] * (pz / pmag)
                        else:
                            a_lat = 0.0

                        # 限幅：真实赛车峰值约 3~4g，超过 6g 必是噪声
                        gl = max(-6.0, min(6.0, a_long / 9.81))
                        gt = max(-6.0, min(6.0, a_lat / 9.81))
                        # 一阶低通：遥测速度有量化噪声，裸差分会有尖刺
                        if self._last_g is not None:
                            al = 0.45
                            gl = al * gl + (1 - al) * self._last_g[0]
                            gt = al * gt + (1 - al) * self._last_g[1]
                        g_force = [round(gl, 3), round(gt, 3), 0.0]
                        self._last_g = [gl, gt]
            self._last_vel = list(velocity)
            self._last_t = recv_time

            # flags 里有 CarOnTrack 标志，比用速度判断「是否在跑」可靠得多
            car_on_track = bool(flags & 0x01)

            # 包类型按**实际长度**推断，而不是一律记 "A"。
            # 心跳切到 B / ~ 后包会变长（316 / 332），状态文件里显示真实类型
            # 便于确认 PS5 是否真的按我们请求的类型发包（协议改动排查用）。
            if n >= 0x148:
                pkt_layout = "~"
            elif n >= 0x130:
                pkt_layout = "B"
            else:
                pkt_layout = "A"
            self.last_layout = pkt_layout
            self.frames_by_layout[pkt_layout] = (
                self.frames_by_layout.get(pkt_layout, 0) + 1)

            return TelemetrySample(
                t=recv_time,
                seq=pack_id,
                speed_kph=speed_kph,
                rpm=engine_rpm,
                gear=gear,
                # InvoGT 的 position 是 [x, y, z]，GT7 坐标系 z 为负
                # 我沿用旧字段名：car_x=x, car_z=z（y 是高度，用不上）
                car_x=position[0],
                car_z=position[2],
                car_y=position[1],
                lap=lap_count,
                throttle=throttle,
                brake=brake,
                wheel_rads=[abs(r) for r in wheel_revs],
                wheel_revs=wheel_revs,
                tyre_temp=tyre_temp,
                susp_height=susp,
                g_force=g_force,
                # ★ 格式 A 就带 position，复盘/超车功能从第一帧可用
                has_coords=True,
                layout=pkt_layout,
                # 附加字段
                velocity=velocity,
                suggested_gear=suggested,
                flags=flags,
                car_on_track=car_on_track,
                paused=bool(flags & 0x02),
                loading=bool(flags & 0x04),
                in_gear=bool(flags & 0x08),
                hand_brake=bool(flags & 0x40),
                laps_in_race=laps_in_race,
                best_lap_ms=best_lap if best_lap != 0xFFFFFFFF else None,
                last_lap_ms=last_lap if last_lap != 0xFFFFFFFF else None,
                gas_level=f32(0x44) if n >= 0x48 else 0.0,
                gas_capacity=f32(0x48) if n >= 0x4C else 0.0,
                powertrain=classify_powertrain(
                    f32(0x48) if n >= 0x4C else 0.0),
                # 能量回收 / 滤波输入：**只在扩展包（~）里有真实值**。
                #
                # 🔴 格式 A 的 0x128 之后就结束了（296 字节），按扩展偏移读 A 包
                #    是越界；而 0x128.. 这段在 A 里是轮速/胎径等，语义完全不同，
                #    绝不能张冠李戴。所以这里用包长严格守门：
                #      A = 296B、B = 316B（+5 float 运动数据）、~ = 332B（+扩展）
                #
                # ⚠️ 扩展段布局来自社区逆向，两大来源有 4 字节分歧
                #    （MacManley/gt7-udp 有 torqueVectors，RaceCrewAI/gt-telem 没有），
                #    且文档自我标注「仍在研究中」。这里采用 MacManley 的
                #    C++ 结构体布局（偏移可逐字段推导，且最新仍在维护）：
                #      0x13C throttleFiltered(u8)  0x13D brakeFiltered(u8)
                #      0x13E u8  0x13F u8
                #      0x140 torqueVectors(f32)    0x144 energyRecovery(f32)
                #      0x148 unknown(f32)
                #    默认心跳是 "A"→ 这段恒为 0；只有显式 --packet-type ~ 才有值。
                energy_recovery=f32(0x144) if n >= 0x148 else 0.0,
                throttle_filtered=(data[0x13C] / 255.0) if n >= 0x13D else 0.0,
                brake_filtered=(data[0x13D] / 255.0) if n >= 0x13E else 0.0,
                car_code=u32(0x124) if n >= 0x128 else 0,
                turbo_boost=f32(0x50) if n >= 0x54 else 0.0,
                # 引擎健康（float32，同一段连续布局）
                oil_pressure=f32(0x54) if n >= 0x58 else 0.0,
                water_temp=f32(0x58) if n >= 0x5C else 0.0,
                oil_temp=f32(0x5C) if n >= 0x60 else 0.0,
                body_height=f32(0x38) if n >= 0x3C else 0.0,
                # 比赛信息（0x80 时钟 u32 / 0x84 发车位 u16 / 0x86 车数 u16）
                time_of_day=u32(0x80) if n >= 0x84 else 0,
                quali_pos=u16(0x84) if n >= 0x86 else 0,
                num_cars=u16(0x86) if n >= 0x88 else 0,
                # 换挡提示转速（u16，单位 rpm）
                min_alert_rpm=float(u16(0x88)) if n >= 0x8A else 0.0,
                max_alert_rpm=float(u16(0x8A)) if n >= 0x8C else 0.0,
            )

        except (struct.error, IndexError, ValueError) as e:
            self.decode_errors += 1
            self.last_warn = f"字段解析失败: {e}"
            return None



class SessionWriter:
    """按场次写 .jsonl。第一行是 header，后续每行一帧遥测。"""

    def __init__(self, outdir: Path, session: Session):
        self.session = session
        self.outdir = outdir
        self.outdir.mkdir(parents=True, exist_ok=True)

        safe_circuit = (session.circuit or "unknown").replace("/", "-").replace(" ", "_")
        stamp = datetime.fromtimestamp(session.started_at).strftime("%Y%m%d_%H%M%S")
        self.path = outdir / f"{stamp}_{safe_circuit}_{session.session_id[:8]}.jsonl"

        self.fh = self.path.open("w", encoding="utf-8", buffering=1)
        self.fh.write(json.dumps(session.to_header(), ensure_ascii=False) + "\n")
        print(f"[recorder] 落盘 -> {self.path}", flush=True)

    def write(self, sample: TelemetrySample) -> None:
        self.fh.write(json.dumps(sample.to_json(), ensure_ascii=False) + "\n")

    def close(self) -> None:
        self.fh.close()
        laps = sorted(self.session.laps_seen)
        if self.session.recording_started and self.session.sample_count > 0:
            print(
                f"[recorder] 场次结束：{self.session.sample_count} 帧 / "
                f"{self.session.last_sample_t - self.session.started_at:.0f} 秒，"
                f"最高 {self.session.max_speed:.1f} km/h，"
                f"圈数 {laps if laps else '无'}",
                flush=True,
            )
        else:
            print(
                f"[recorder] 丢弃未开跑的会话（车辆始终静止）",
                flush=True,
            )
        print(f"[recorder] 文件：{self.path}", flush=True)


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------

class Recorder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.decoder = Decoder()
        self.session: Session | None = None
        self.writer: SessionWriter | None = None
        self.running = True
        self.last_warn_at = 0.0
        self._status_buf: deque[dict[str, Any]] = deque(maxlen=600)
        # 见到过的来源 IP，用于状态展示
        self._seen_sources: set[str] = set()
        # 连续多少帧速度达到阈值 → 判定车辆真的在跑
        self._moving_frames = 0
        # 是否已打印过「忽略其它 IP」的提示，避免刷屏
        self._logged_other_ip = False
        # 状态文件上次写入时间（用于节流）
        self._last_status_write = 0.0
        # 最近一次收到包的来源 IP（心跳要发回去）
        self._last_source: str | None = None
        # 上一帧内容指纹（跳过游戏暂停时的重复帧）
        self._last_sig: tuple | None = None
        # 被跳过的重复帧计数
        self.duplicate_frames = 0
        # 按端口统计收包数（诊断用：排查 176Hz 异常速率）
        self._recv_by_port: dict[str, int] = {}
        # —— 行车轨迹（用于画轨迹图）——
        # 每点存 [x, z, G力大小]，按时间降采样（默认 100ms 一个点）。
        # 全量存 60Hz 的点会太大（一圈 2 分钟 = 7200 点），降采样后
        # 一圈约 1200 点，足够画出平滑轨迹。
        self._path: list[list[float]] = []
        self._path_last_t = 0.0
        # —— G-G 散点（横向 G vs 纵向 G）——
        # 每点存 [横向, 纵向]，用于画「抓地力圆」图。
        self._gg: list[list[float]] = []
        self._gg_last_t = 0.0
        # 轨迹/G-G 上次写进状态文件的时刻（它们体积大，单独降频写）
        self._last_geo_write = 0.0
        # —— 场次边界状态 ——
        # 车辆离开赛道的起始时刻（0 = 当前在赛道上）
        self._off_track_since = 0.0
        # 上一帧的赛道判定（用于只在状态变化时打日志）
        self._was_on_track = False
        # 回收站清理计时（主循环每小时查一次，超期文件删除）
        self._last_trash_check = 0.0
        # —— 每圈油耗（会话级）——
        # GT7 不直接报油耗，用「圈首油量 - 圈末油量」差分估计。
        # lap_fuel: [[第几圈, 该圈油耗百分比], ...]
        self.lap_fuel: list[list] = []
        self._fuel_mark: float | None = None   # 圈首油量标记
        # —— 每圈成绩（会话级）——
        # GT7 只报「最快圈 / 上一圈」两个字段，没有完整历史；
        # 想显示每圈列表就得自己攒：lastLapTime 值变化 = 刚跑完一圈。
        self.lap_times: list[list] = []      # [[第几圈, 毫秒], ...]
        self._last_lap_ms_seen = 0           # 去重：同一圈会连续上报几百帧
        # 上一帧的圈数（用于检测「圈数重置 = 新比赛」）
        self._prev_lap = -1
        # 本容器本次启动已记录的场次数
        self.session_count = 0

    def log(self, msg: str, force: bool = False) -> None:
        # 警告最多每 30 秒打一次，避免刷屏
        now = time.time()
        if not force and now - self.last_warn_at < 30:
            return
        print(f"[recorder] {msg}", flush=True)
        self.last_warn_at = now

    # -- 来源 IP 过滤 ----------------------------------------------------

    def _source_allowed(self, ip: str) -> bool:
        """
        按--ps5 参数决定是否接受这个来源的包。

        为什么需要手动指定：某些网络（路由器开了 AP 隔离、多 VLAN）
        下 PS5 的广播收不到，或者局域网里有多个设备在广播。
        锁定 IP 可以排除干扰。

        默认 auto：接受任意来源——因为 UDP 广播本来就能自动识别来源，
        收到包时 addr[0] 就是发送方，不需要预先知道 PS5 的 IP。
        """
        target = (self.args.ps5 or "auto").strip()
        if target.lower() == "auto":
            return True
        return ip == target

    def stop(self, sig: Any, frm: Any) -> None:
        print("\n[recorder] 收到信号，准备退出…", flush=True)
        self.running = False

    def run(self) -> int:
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        outdir = Path(self.args.output).resolve()
        outdir.mkdir(parents=True, exist_ok=True)

        # 同时监听两个端口。格式 A（33739）始终有包，
        # 格式 B/C（33740）只在部分场景有。
        s1 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s1.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s1.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s1.bind(("0.0.0.0", PORT_HEARTBEAT))
        # 🔴 必须非阻塞！踩过的坑：原先 setblocking/timeout(0.5)，
        #    而 33739 端口平时几乎没数据 → recvfrom 每次干等 0.5 秒，
        #    期间 33740 的包全堆在内核缓冲里，下一轮被**一次性读出**，
        #    于是几十帧共享同一个时间戳（实测 12 帧只差 0.0001 秒）。
        #    后果：G 力算不出（dt≈0）、帧率虚高 3 倍（实测假 176Hz，真 60Hz）。
        s1.setblocking(False)

        s2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s2.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s2.bind(("0.0.0.0", PORT_ADVANCED))
        s2.setblocking(False)

        ps5_mode = (self.args.ps5 or "auto").strip()
        print("=" * 66, flush=True)
        print("GT7 遥测接收服务", flush=True)
        print(f"  监听 0.0.0.0:{PORT_HEARTBEAT} (格式A) / :{PORT_ADVANCED} (格式B/C)",
              flush=True)
        print(f"  输出目录 {outdir}", flush=True)
        if ps5_mode.lower() == "auto":
            print("  PS5 来源: 自动（接受任意来源）", flush=True)
        else:
            print(f"  PS5 来源: 仅接受 {ps5_mode}", flush=True)
        print(f"  赛道判定: 速度 ≥ {self.args.track_min_speed} km/h 且 ≥ {self.args.track_min_frames} 帧",
              flush=True)
        print(f"  心跳保活: 每 {self.args.heartbeat_interval:.0f} 秒（必需，缺失会导致 PS5 停止发包）",
              flush=True)
        print(f"  场次切分: 离开赛道 {self.args.off_track_timeout:.0f} 秒算一场结束"
              f"（兜底：{self.args.session_gap:.0f} 秒无包）", flush=True)
        print("  等待 PS5 进入赛道…", flush=True)
        print("=" * 66, flush=True)

        # —— 启动 Salsa20 解密器（必需）——
        # 🔴 GT7 真机发的包 100% 是 Salsa20 加密的。
        #   没有解密器就一个数据都解不出来。
        dec_path = self.args.decryptor
        if dec_path and Path(dec_path).exists():
            self.decoder._args_path_hint = dec_path
            self.decoder._start_decryptor(dec_path, log=self.log)
        else:
            self.decoder.last_warn = (
                f"找不到解密器 {dec_path}。GT7 遥测是 Salsa20 加密的，"
                f"没有它无法解码任何数据。"
            )
            self.log(self.decoder.last_warn, force=True)

        if self.args.probe:
            self.log("发送 Hello 探测包…")
            discover_ps5(timeout=2.0)

        last_active = 0.0
        last_hb = time.time()      # 上次发心跳的时间
        hb_count = 0# 已发送心跳次数
        batch: list[tuple[bytes, float]] = []   # (原始包, 到达时刻)
        batch_addr = "0.0.0.0"

        while self.running:
            # —— 用 select 等待，最多 20ms ——
            #
            # 为什么要用 select 而不是给每个 socket 设 timeout：
            # 33739 端口平时几乎没有流量，若用阻塞 recvfrom + timeout，
            # 会在这个端口上白等一整个超时周期，期间 33740 的包全部堆积，
            # 下一轮被一次性读出 → 时间戳成簇 → G 力算不出、帧率虚高。
            # select 让我们「谁有数据就读谁」，且等待时间可控。
            try:
                ready, _, _ = select.select([s1, s2], [], [], 0.02)
            except (OSError, ValueError):
                ready = []

            for sock in ready:
                tag = "A" if sock is s1 else "B/C"
                got = 0
                # 把该 socket 当前可读的包**全部抽干**（非阻塞，读不到就停）
                # 每轮上限 256，避免一次占用过久导致另一个端口饿死
                while got < 256:
                    try:
                        data, addr = sock.recvfrom(2048)
                    except (BlockingIOError, socket.timeout):
                        break          # 队列空了
                    except OSError:
                        break

                    got += 1

                    # —— 来源 IP 过滤（--ps5 auto 时放行全部）——
                    if not self._source_allowed(addr[0]):
                        if not self._logged_other_ip:
                            self._logged_other_ip = True
                            self.log(
                                f"已忽略来自 {addr[0]} 的包"
                                f"（当前锁定 {ps5_mode}，如需接收请改 --ps5 auto）",
                                force=True,
                            )
                        continue

                    # 记录见过的来源 IP（仪表盘显示用）
                    self._seen_sources.add(addr[0])
                    self._last_source = addr[0]

                    # 收进本轮批量缓冲。
                    # 🔴 必须**逐包记录到达时刻**，不能整批共用一个时间戳！
                    # 踩过的坑：原先只存 data，批处理时统一传一个 now，
                    # 结果同一批内几十帧的时间戳完全相同（实测 25 帧同一 t）。
                    # 后果：
                    #   · G 力恒为 0 —— 差分算加速度时 dt=0，被我的守卫跳过
                    #   · 所有基于时间的分析（圈速、帧率、回放时间轴）全部失真
                    # 正确做法：UDP 包从 socket 读出时就打时间戳。
                    batch.append((data, time.time()))
                    batch_addr = addr[0]
                    got += 1
                    # 按端口统计（诊断用：GT7 名义 60Hz，实测偏高需查明）
                    self._recv_by_port[tag] = self._recv_by_port.get(tag, 0) + 1

            # —— 批量处理本轮收到的包 ——
            if batch:
                now = time.time()
                if self._last_source:
                    batch_addr = self._last_source
                samples = self.decoder.decode_many(
                    [b[0] for b in batch], [b[1] for b in batch]
                )
                # 🔴 处理完**必须清空**！这是本项目最隐蔽的 bug 之一。
                # batch 定义在 while 之外，若不清空会一直累积，
                # 于是每一轮都把**历史上所有包重新处理一遍**：
                #   · 同一帧被反复落盘 → 47 秒跑出 28 万帧（实际仅约 2850 个包）
                #   · 时间戳大量重复（旧包带着原始时间戳被重新写入）
                #   · 文件暴涨（200MB / 47 秒）、CPU 白耗、内存持续增长
                # 症状很像「数据重复」，但根因是循环里漏了 clear()。
                batch.clear()
                for smp in samples:
                    if smp is not None:
                        # 用该帧自己的到达时刻，而不是批次时间
                        self._handle_sample(smp, batch_addr, smp.t)
                last_active = now

            # —— 心跳保活 ——
            # ⚠️ 这是个容易漏但**必须**做的机制。
            # GT7 要求客户端定期发心跳（内容就是一个字节 "A"），
            # 否则 PS5 会认为客户端已断开、**停止发送遥测**。
            # 社区实现都是「每收 100 个包或超时时发一次」
            # （见 zetetos/gt-telemetry、snipem/go-gt7-telemetry）。
            #
            # 症状：刚启动能收几秒，之后 PS5 就不再发了。
            # 原因：没发心跳，被 PS5 单方面判定断连。
            #
            # ⚠️ 广播并不可靠——某些网络配置下广播收不到。
            #    所以**同时单播**给指定的 PS5 IP（--ps5 参数）。
            #    这是实测最稳的做法。
            if time.time() - last_hb >= self.args.heartbeat_interval:
                hb_count += 1
                # 目标列表：已知来源 + 指定的 PS5 + 广播（去重）
                targets: list[tuple[str, int]] = []
                for ip in (
                    self._last_source,
                    None if ps5_mode.lower() == "auto" else ps5_mode,
                ):
                    if ip and (ip, PORT_HEARTBEAT) not in targets:
                        targets.append((ip, PORT_HEARTBEAT))
                if ("255.255.255.255", PORT_HEARTBEAT) not in targets:
                    targets.append(("255.255.255.255", PORT_HEARTBEAT))

                for tgt_ip, tgt_port in targets:
                    try:
                        # 心跳字符决定 PS5 回哪种包：
                        #   "A" → 296 字节基础包（默认）
                        #   "B" → 316 字节（+运动数据）  "~" → 332 字节（+能量回收）
                        s1.sendto(self.args.packet_type.encode("ascii"),
                                  (tgt_ip, tgt_port))
                    except OSError:
                        pass
                last_hb = time.time()
                if hb_count == 1:
                    self.log(
                        f"已启用心跳保活（每 {self.args.heartbeat_interval:.0f} 秒"
                        f"，目标 {len(targets)} 个，包类型 {self.args.packet_type}）"
                        " —— 缺少它 PS5 会停止发遥测",
                        force=True,
                    )

            # —— 场次边界（一）：离开赛道够久 → 结束本场 ——
            # 🔴 这是场次切分的主要依据。GT7 退回菜单/结算后仍持续发遥测，
            #    包流不断，只看「收不到包」会把两场比赛并进一个文件。
            #    ⚠️ 必须放在主循环按墙上时钟检查，不能放在 _handle_sample 里：
            #       菜单里重复帧会被去重拦截，帧内的检查根本不会执行。
            self._check_off_track(time.time())

            # 回收站清理：每小时检查一次 _trash 里超期的已删除场次
            if time.time() - self._last_trash_check >= 3600:
                self._last_trash_check = time.time()
                self._cleanup_trash()

            # 掉线检测：超过阈值无包则认为会话结束
            if self.session and now_is_set(last_active):
                idle = time.time() - last_active
                if idle > self.args.session_gap:
                    if self.session.recording_started:
                        self.log(
                            f"{self.args.session_gap:.0f} 秒无数据，"
                            f"判定场次结束（PS5 可能已退出赛道）", force=True
                        )
                    self._end_session()
                    last_active = 0.0

            # 队列空时才让出 CPU（有数据时连续抽干，见上方批量读取）
            time.sleep(0.002)

        self._end_session()
        s1.close()
        s2.close()
        print("[recorder] 已退出", flush=True)
        return 0

    def _handle_sample(
        self, sample: TelemetrySample, src_ip: str, now: float
    ) -> None:
        """
        处理一帧**已解密**的采样：赛道判定 + 落盘 + 状态文件。

        ⚠️ 参数从 (data, addr, now) 变成 (sample, src_ip, now)——
           因为解密已提到批处理层（decode_many）统一做了，
           每包起一个解密进程的代价承受不起（60Hz × fork）。
        """
        # —— 赛道判定 ——
        # GT7 在菜单、停车场、赛道加载界面也会广播遥测（速度 0）。
        # 如果全落盘，一场比赛会混入大量静止帧，且容易被误认为「正在采集」。
        #
        # ⚠️🔴 这里踩过一个**代价很大**的坑，务必看清：
        #
        #   `car_on_track`（flags bit0）看起来是「车在赛道上」的权威标志，
        #   但我实测发现**它并不可靠**——真机跑图时拿到过
        #       flags=0x0198  speed=69.6km/h  car_on_track=False
        #   即「车明明在跑，bit0 却没置位」（0x0198 只有
        #   InGear|Turbo|Lights|HighBeam）。而另一些时刻又是 flags=0x0819
        #   （bit0 置位）。**同一台 PS5 上这个位会时有时无。**
        #
        #   我曾经只看别处出现过 flags=0x0819 就断定「该位可靠」，
        #   据此改成「flags!=0 时完全信任 car_on_track」——
        #   结果真机上一整场比赛都没被记录（录制从不启动，
        #   地图/G-G 空白、本场极速恒为 0）。
        #
        #   结论：**两个条件取「或」**，任一成立就算在赛道上。
        #   速度判定的副作用（回放也会被算进来）远小于
        #   「整场比赛丢数据」的代价。
        on_track = bool(sample.car_on_track) or (
            sample.speed_kph >= self.args.track_min_speed
        )

        if on_track:
            self._moving_frames += 1
            self._off_track_since = 0.0
        else:
            self._moving_frames = 0
            if self._off_track_since == 0.0:
                self._off_track_since = now

        # 赛道状态变化时打一行日志（含原始 flags 与速度），
        # 便于事后排查「为什么没记录/为什么切场次」。
        if on_track != self._was_on_track:
            self.log(
                f"赛道状态 → {'在赛道上' if on_track else '离开赛道'}"
                f"（speed={sample.speed_kph:.0f}km/h flags=0x{sample.flags:04X}"
                f" car_on_track={sample.car_on_track}）",
                force=True,
            )
            self._was_on_track = on_track

        # —— 场次边界（一）：离开赛道够久 → 结束本场 ——
        # 检查放在 run() 主循环里按**墙上时钟**做，而不是这里：
        # 🔴 踩过的坑：暂停/菜单时 PS5 重复发同一帧，全部被去重拦截，
        #    这里的代码根本不会执行 → 超时判定永远不触发。
        #    主循环的检查不依赖帧流，去重不影响。

        # —— 场次边界（二）：圈数重置 → 新比赛 ——
        # 补充防线：如果玩家原地重开一局（没离开赛道），
        # car_on_track 一直是 True，上面那条不会触发。
        # 圈数从较大值掉回 0/1 说明是新的一局。
        #
        # ⚠️ 必须同时满足「当前在赛道上」，否则在菜单里圈数归 0
        #    也会被当成新比赛，导致场次被切得稀碎。
        lap_now = sample.lap
        if (
            self.args.off_track_timeout > 0
            and on_track
            and self.session is not None
            and self.session.recording_started
            and self._prev_lap >= 3
            and lap_now <= 1
        ):
            self.log(
                f"圈数从 {self._prev_lap} 重置为 {lap_now} → 判定为新比赛",
                force=True,
            )
            self._end_session()
        self._prev_lap = lap_now

        # —— 去重：跳过与上一帧完全相同的帧 ——
        #
        # 游戏暂停时物理冻结，但 PS5 仍以 60Hz 重复发送**同一份数据**
        # （实测暂停期间 99% 的帧内容完全相同，只有包序号在涨）。
        # 全落盘的后果：
        #   · 文件暴涨（实测一份 1.7GB，绝大部分是重复帧）
        #   · 仪表盘的历史缓冲被重复值填满，曲线变成没意义的直线
        # 所以内容完全一致的连续帧只保留第一帧。
        sig = (
            round(sample.speed_kph, 2), round(sample.rpm, 1), sample.gear,
            round(sample.throttle, 3), round(sample.brake, 3),
            round(sample.car_x, 3), round(sample.car_z, 3),
            tuple(round(t, 1) for t in sample.tyre_temp),
            sample.flags,
        )
        if sig == self._last_sig:
            self.duplicate_frames += 1
            return
        self._last_sig = sig

        # 首帧 → 建会话（先建但不写盘，等确认在跑再写）
        if self.session is None:
            sid = f"{int(now):x}{src_ip}"
            self.session = Session(
                session_id=sid,
                started_at=now,
                source_ip=src_ip,
                recording_started=False,
            )
            self.log(f"收到 {src_ip} 的遥测包，等待车辆移动…", force=True)

        # 还没确认在跑：累计够帧数才正式开始落盘
        if not self.session.recording_started:
            if self._moving_frames < self.args.track_min_frames:
                # 未达阈值，只更新状态文件让仪表盘显示「待命」
                if self.args.status_file:
                    self._write_status(sample, now)
                return

            # 达到阈值 → 正式开始
            # 🔴 上一场的轨迹 / 圈速在这里清空（而不是场次结束时）：
            #    这样跑完回菜单后，仪表盘仍显示刚跑完的完整轨迹与圈速，
            #    直到下一场开跑才重置。
            self._path.clear()
            self._gg.clear()
            self._path_last_t = 0.0
            self._gg_last_t = 0.0
            self.lap_times.clear()
            self.lap_fuel.clear()
            # 本场动力类型以首帧为准（同一场次不会换车），写进会话头
            self.session.powertrain = sample.powertrain
            self.session.max_energy_recovery = 0.0
            # 🔴 _last_lap_ms_seen 要初始化为**当前值**而不是 0：
            #    否则录制启动帧自己就会触发一次「冲线」（0 → 有值），
            #    lap_fuel 第一条恒为 0.0 的假圈。
            self._last_lap_ms_seen = sample.last_lap_ms
            self._fuel_mark = sample.gas_level   # 圈首油量从本场第一帧记起
            self._prev_lap = -1
            self.session.recording_started = True
            self.session.started_at = now      # 用开跑时刻作为场次起点
            self.session.source_ip = src_ip
            self.session_count += 1
            self.writer = SessionWriter(Path(self.args.output), self.session)
            self.log(
                f"▶ 第 {self.session_count} 场：检测到车辆在赛道上，开始采集"
                f"（已跳过前 {self._moving_frames} 帧未上赛道的数据）",
                force=True,
            )

        # ---- 正式采集 ----
        self.session.laps_seen.add(sample.lap)
        self.session.max_speed = max(self.session.max_speed, sample.speed_kph)
        # 能量回收峰值（仅扩展包有值；默认格式 A 恒为 0，不会污染统计）
        if sample.energy_recovery > self.session.max_energy_recovery:
            self.session.max_energy_recovery = sample.energy_recovery

        # —— 每圈成绩：lastLapTime 变化 = 刚冲线 ——
        # 不用圈号判断（不同模式下起点不一致），用「值变化」最稳：
        # GT7 对同一个 lastLapTime 会连续上报几百帧，必须去重。
        if sample.last_lap_ms and sample.last_lap_ms != self._last_lap_ms_seen:
            # 🔴 能耗只在冲线时结算：_fuel_mark 是上一冲线时刻的能量读数，
            #    差值 = 这一圈消耗了多少。若放在每帧执行，标记每帧被刷新，
            #    差值永远只剩一帧的消耗（≈0），且 lap_fuel 会被灌满垃圾。
            #
            # ⚠️ 这条对油车和电车**通用**——因为 gas_level 在两种车上
            #    都是「剩余能量」读数：燃油车是百分比（0~100），
            #    纯电车是剩余电量 kWh。所以 lap_fuel 里的数字对电车就是
            #    「每圈耗多少 kWh」，字段名沿用但单位随 powertrain 变化。
            if self._fuel_mark is not None:
                self.lap_fuel.append([len(self.lap_times) + 1,
                                      round(self._fuel_mark - sample.gas_level, 2)])
                if len(self.lap_fuel) > 60:
                    self.lap_fuel = self.lap_fuel[-60:]
            self._fuel_mark = sample.gas_level
            self._last_lap_ms_seen = sample.last_lap_ms
            self.lap_times.append([len(self.lap_times) + 1, sample.last_lap_ms])
            if len(self.lap_times) > 60:      # 防止超长耐力赛撑爆状态文件
                self.lap_times = self.lap_times[-60:]
        self.session.sample_count += 1
        self.session.last_sample_t = now
        self.session.state = SESSION_ACTIVE

        assert self.writer is not None
        self.writer.write(sample)

        # —— 累积行车轨迹与 G-G 散点（供仪表盘画轨迹图 / 抓地力圆）——
        # 🔴 只在赛道上时累积：菜单/结算画面里车的上报位置是固定点
        #    （实测 21:45 停表后菜单帧把 path 从 43 点灌到 215 点，
        #    全堆在同一个坐标上），会把轨迹污染成一团。
        if on_track:
            self._accumulate_geo(sample, now)

        # 把最新状态写进状态文件，供 Web 仪表盘读取。
        # ⚠️ 为什么不直接在内存里共享？接收器和仪表盘是两个进程
        #    （docker 里可以是两个容器，或同容器两个线程）。
        #    走文件最简单可靠，且不触碰 UDP 主循环的性能。
        #    写文件只取最后一帧，开销可忽略。
        if self.args.status_file:
            self._write_status(sample, now)

        if self.session.sample_count % 600 == 0:
            self.log(
                f"已采集 {self.session.sample_count} 帧 / "
                f"{now - self.session.started_at:.0f}s，"
                f"最高 {self.session.max_speed:.0f} km/h"
            )
            # 打印布局分布——这是判断「收到的是哪种格式」最快的办法。
            # 如果只有 A，说明 PS5 发的包不含车身坐标，
            # 复盘功能里的赛道定位需要另找数据源。
            dist = self.decoder.frames_by_layout
            self.log(f"包格式分布: {dist}（C=含车身坐标，B=含胎温，A=基础）")

    # -- 轨迹与 G-G 累积 --------------------------------------------------

    # 轨迹点上限（约 4000 点 = 10Hz 下 6.6 分钟；超了丢最早的）
    path_cap = 4000
    # G-G 散点上限
    gg_cap = 1200

    def _accumulate_geo(self, sample: TelemetrySample, now: float) -> None:
        """
        累积**行车轨迹**（画轨迹图用）和 **G-G 散点**（画抓地力圆用）。

        为什么要降采样并设上限：
        60Hz 全量存，跑 10 分钟就是 36000 个点，状态文件会胀到几 MB、
        仪表盘渲染也会卡。降到 10Hz 后轨迹依然平滑，体积降到 1/6。
        上限用「丢最早的」而不是停止追加，这样长时间跑图能看到最新路段。
        """
        # —— 赛道轨迹：[x, z, G力大小] ——
        if now - self._path_last_t >= 0.1:
            gmag = math.hypot(sample.g_force[1], sample.g_force[0])
            self._path.append([
                round(sample.car_x, 1),
                round(sample.car_z, 1),
                round(gmag, 2),
            ])
            self._path_last_t = now
            if len(self._path) > self.path_cap:
                del self._path[: len(self._path) - self.path_cap]

        # —— G-G 散点：[横向 G, 纵向 G] ——
        if now - self._gg_last_t >= 0.06:
            self._gg.append([
                round(sample.g_force[1], 2),
                round(sample.g_force[0], 2),
            ])
            self._gg_last_t = now
            if len(self._gg) > self.gg_cap:
                del self._gg[: len(self._gg) - self.gg_cap]

    def _write_status(self, sample: TelemetrySample, now: float) -> None:
        """
        写状态文件（原子替换，避免仪表盘读到半截 JSON）。

        ⚠️ 为什么要带一个环形缓冲，而不是只写最后一帧？
            遥测是 60Hz，仪表盘按 10Hz 轮询状态文件。
            如果只写最后一帧，仪表盘每次只能新增 1 帧到自己的缓冲，
            60Hz 的数据被 10Hz 的轮询采到 → **曲线只剩 1/6 的点**。
            早期版本就是这样，history 数组里只有 1 帧。

        解法：接收器在状态文件里自带一个 deque（最近 N 帧），
        仪表盘拿到后整批覆盖自己的缓冲。

        ⚠️ 落盘必须节流：仪表盘只需 10Hz，遥测是 60Hz。
            每帧都写 = 每秒 60 次写 20KB JSON，既浪费也拖慢收包主循环
            （会加剧队列堆积）。这里限制最多 20Hz 写状态文件，
            jsonl 正式数据仍然每帧都写（那个不能丢）。

            🔴 踩过的坑：节流的`return` 原本放在**缓冲追加之前**，
               导致曲线缓冲也被限流 → history 只剩 10 帧（应为 600）。
               缓冲追加必须**每帧都做**，只有写文件才节流。
        """
        # 当前是否在赛道上（仪表盘据此显示「在场/不在场」）
        # 口径必须与 _handle_sample 完全一致，否则界面会和实际记录不一致。
        # 同样是「或」逻辑——详见 _handle_sample 里关于 car_on_track 不可靠的说明。
        on_track_now = bool(sample.car_on_track) or (
            sample.speed_kph >= self.args.track_min_speed
        )

        # —— 先无条件追加缓冲（每帧都要，曲线靠它）——
        self._status_buf.append({
            "t": round(sample.t, 3),
            "s": round(sample.speed_kph, 1),
            "r": round(sample.rpm, 0),
            "g": sample.gear,
            "th": round(sample.throttle, 2),
            "bk": round(sample.brake, 2),
            "lap": sample.lap,
            "wt": [round(x, 0) for x in sample.tyre_temp],
            "wf": [round(x, 0) for x in sample.wheel_rads],
            "gf": [round(x, 2) for x in sample.g_force],
        })

        # —— 再节流写文件（最多 20Hz）——
        if now - self._last_status_write < 0.05:
            return
        self._last_status_write = now

        try:
            payload = {
                "t": now,
                "frame": sample.to_json(),
                "frames_total": self.session.sample_count,
                "session_start": self.session.started_at,
                # 被跳过的重复帧（游戏暂停时 PS5 会 60Hz 重复发同一帧）
                "duplicate_frames": self.duplicate_frames,
                # 按端口收包统计（诊断 176Hz 异常用）
                "recv_by_port": dict(self._recv_by_port),
                # 每圈成绩 [[第几圈, 毫秒], ...]（会话级，仪表盘画列表用）
                "lap_times": list(self.lap_times),
                "lap_fuel": list(self.lap_fuel),
                # 本场极速（会话级累计，不随历史缓冲滚动而变）
                # 仪表盘原先显示的「极速」是从 600 帧窗口算的，
                # 窗口一滚走数值就变了 —— 会让人以为读数不对。
                "session_max_speed": round(self.session.max_speed, 1),
                "layouts": dict(self.decoder.frames_by_layout),
                "warning": self.decoder.last_warn,
                "has_coords": sample.has_coords,
                "decoder_errors": self.decoder.decode_errors,
                # 动力类型与能量回收峰值（本场，随首帧确定）
                "powertrain": self.session.powertrain,
                "max_energy_recovery": round(self.session.max_energy_recovery, 3),
                "packet_type": self.args.packet_type,
                # 紧凑历史：字段名缩写以减小体积
                "history": list(self._status_buf),
                # —— 新增：状态元信息 ——
                # recording：是否已开始正式录制（vs 仅收到包但车没动）
                "recording": self.session.recording_started,
                # 本次启动已记录的场次数 + 是否在赛道上（仪表盘显示用）
                "session_count": self.session_count,
                "on_track": on_track_now,
                "source_ips": sorted(self._seen_sources),
                "ps5_filter": self.args.ps5 or "auto",
                # 距离达到「判定在跑」还差几帧（用于仪表盘提示）
                "moving_frames": self._moving_frames,
                "track_min_frames": self.args.track_min_frames,
            }

            # —— 轨迹与 G-G 散点：体积大，单独降频到 2Hz ——
            #
            # 轨迹最多 4000 点、G-G 最多 1200 点，全塞进 20Hz 的状态文件
            # 会让写入量翻十几倍（每秒几百 KB），拖慢收包主循环。
            # 地图和抓地力圆本来就是慢变量，2Hz 足够。
            # 仪表盘侧对「字段缺失」保持上一次的值，所以不会闪。
            if now - self._last_geo_write >= 0.5:
                self._last_geo_write = now
                payload["path"] = list(self._path)
                payload["gg"] = list(self._gg)

            tmp = self.args.status_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.args.status_file)   # 原子替换
        except OSError:
            # 状态文件写失败不该影响采集，静默跳过
            pass

    def _cleanup_trash(self, now: float | None = None) -> int:
        """清理回收站：删除 data/_trash/ 里超过保留期的场次文件。

        ⚠️ _trash 里的文件已经是「用户删除」状态，这里只是延迟真删。
        保留期 --trash-retention-days（默认 30 天，0 = 永不清理）。
        返回删除的文件数。
        """
        now = time.time() if now is None else now
        # 保留期优先读 data/settings.json（历史场次页可调，改完即生效），
        # 没有该文件或值非法时回退到命令行参数。
        retention = self.args.trash_retention_days
        try:
            sf = Path(self.args.output) / "settings.json"
            v = json.loads(sf.read_text(encoding="utf-8")).get("trash_retention_days")
            if isinstance(v, (int, float)) and 0 <= v <= 3650:
                retention = float(v)
        except Exception:
            pass
        if retention <= 0:
            return 0
        trash = Path(self.args.output) / "_trash"
        if not trash.is_dir():
            return 0
        cutoff = now - retention * 86400
        removed = 0
        for f in sorted(trash.glob("*.jsonl")):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            self.log(
                f"回收站清理：删除 {removed} 个超过 {retention:.0f} 天的已删除场次",
                force=True,
            )
        return removed

    def _check_off_track(self, now: float) -> None:
        """场次边界检查：离开赛道持续 off_track_timeout 秒 → 结束本场。

        ⚠️ 必须由 run() 主循环按**墙上时钟**周期调用，不能放进
        _handle_sample：菜单/暂停时重复帧全被去重拦截，帧内的检查
        根本不会执行（实测踩过：菜单里停 20 秒，场次永远不结束）。
        """
        if (self.args.off_track_timeout > 0
                and self.session is not None
                and self.session.recording_started
                and self._off_track_since > 0
                and now - self._off_track_since >= self.args.off_track_timeout):
            self.log(
                f"已离开赛道 {now - self._off_track_since:.0f} 秒 → "
                f"场次结束（再进赛道会自动开新场次）",
                force=True,
            )
            self._end_session()
            self._off_track_since = 0.0
            self._moving_frames = 0

    def _end_session(self) -> None:
        if self.writer:
            self.writer.close()
        self.writer = None
        self.session = None
        # 重置指纹：新场次的第一帧不该因为与上场末帧相同而被跳过
        self._last_sig = None
        # 🔴 注意：这里**故意不清空** _path/_gg/lap_times！
        #    踩过的坑：原先在这里清空，结果「跑完回菜单」的瞬间
        #    状态文件就被写成空轨迹——刚跑完的轨迹图和圈速列表
        #    在仪表盘上直接消失（DC 报「地图不绘制了」的真凶）。
        #    清空动作已移到「新场次开始」处（见 _handle_sample）。
        # 离赛道计时清零（新场次重新开始判定）
        self._off_track_since = 0.0


def now_is_set(x: Any) -> bool:
    return isinstance(x, float) and x > 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="GT7 遥测接收服务 —— 常驻监听并按场次落盘",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 自动识别任意来源的 PS5（推荐先用这个看能否收到）
  python gt7-recorder.py --output ./data

  # 锁定特定 PS5（自动获取失败、或局域网多设备干扰时）
  python gt7-recorder.py --output ./data --ps5 192.168.43.100

  # 调整「判定在跑」的速度阈值（默认 15 km/h）
  python gt7-recorder.py --output ./data --track-min-speed 25

  # 更严格的赛道判定：需要连续 120 帧都在动才算开跑
  python gt7-recorder.py --output ./data --track-min-frames 120

会话如何自动切分：
  · 收到包但车没动（菜单/停车场/加载中）→ 只更新仪表盘，**不落盘**
  · 速度连续 N 帧 ≥ 阈值 → 判定开跑，自动建新场次文件
  · 超过 --session-gap 秒收不到任何包 → 结束场次
  → 全自动，**不需要手动 start/stop**
        """,
    )
    p.add_argument(
        "-o", "--output", default="./data",
        help="落盘目录（按场次分文件为 .jsonl），默认 ./data",
    )
    p.add_argument(
        "--ps5", default="auto",
        help="PS5 来源 IP。auto（默认）= 接受任意来源；"
             "也可指定如 192.168.43.100 来精确锁定",
    )
    p.add_argument(
        "--track-min-speed", type=float, default=15.0,
        help="判定车辆在行驶的速度阈值 km/h，默认 15。"
             "低于此值视为静止/菜单，不落盘",
    )
    p.add_argument(
        "--track-min-frames", type=int, default=30,
        help="需要连续多少帧达到速度阈值才判定开跑，默认 30（约 0.5 秒）",
    )
    p.add_argument(
        "--session-gap", type=float, default=300.0,
        help="多少秒收不到包就结束场次，默认 300。"
             "⚠️ 不要设太短：玩家暂停游戏看菜单时遥测仍在发，"
             "只有真正退出赛道才会断流",
    )
    p.add_argument(
        "--trash-retention-days", type=float, default=30.0,
        help="回收站（data/_trash/）里的已删除场次保留多少天，默认 30。"
             "每天自动清理一次超期文件；设 0 = 永不自动清理。"
             "注意 _trash 里的场次已是「删除」状态，这里只是延迟真删。",
    )
    p.add_argument(
        "--off-track-timeout", type=float, default=15.0,
        help="离开赛道（car_on_track 变 False）持续多少秒算场次结束，默认 15。"
             "这是**主要的场次切分依据**：回到菜单/换赛道/结束比赛都会被识别，"
             "从而让每场比赛单独成文件。设 0 可关闭",
    )
    p.add_argument(
        "--heartbeat-interval", type=float, default=5.0,
        help="心跳保活间隔秒数，默认 5。"
             "⚠️ 必须定期向 PS5 发 'A' 心跳，否则 PS5 会停止发送遥测",
    )
    p.add_argument(
        "--packet-type", default="A", choices=["A", "B", "~"],
        help="请求的遥测包类型（改心跳字符），默认 A（296 字节，最稳）。"
             "B = 运动数据（316 字节，多 sway/heave/surge，Sport 模式不可用）；"
             "~ = 扩展数据（332 字节，多能量回收/滤波输入，回放不可用）。"
             "⚠️ B/~ 的扩展段是社区逆向字段、仍在研究中，且非所有模式都支持；"
             "普通使用保持 A 即可。纯电车电量在 A 包里就有，无需切包。",
    )
    p.add_argument(
        "--probe", action="store_true",
        help="启动时发送 Hello 探测包，用于某些不主动广播的网络",
    )
    p.add_argument(
        "--decryptor", default="/app/gt7-decrypt",
        help="Salsa20 解密器路径（必需）。"
             "GT7 遥测包是加密的，没有它无法解码任何数据",
    )
    p.add_argument(
        "--status-file", default=None,
        help="状态文件路径（供 Web 仪表盘读取），例：/data/status.json",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="详细日志",
    )
    args = p.parse_args()

    rec = Recorder(args)
    try:
        return rec.run()
    except KeyboardInterrupt:
        rec._end_session()
        return 0


if __name__ == "__main__":
    sys.exit(main())