#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GT7 原始遥测抓包器 —— 给第 4 批（A8 / A3 / A7）准备数据。

为什么要有这个工具（而不是直接用 recorder）
-------------------------------------------
recorder 是"解析完再落盘"：字段没解码，抓下来就等于没抓。
而 C 包（368B）目前**完全没有解码代码**，~ 包的扩展段偏移也**没验证过**。
所以第 4 批的第一步必须是**先拿原始包**，离线分析出真实偏移，再回头改解码器。

这个工具只做一件事：把每个 UDP 包原样存下来（加密 hex + 解密 hex + 长度 +
时间戳），并实时打印**长度分布**——那正是 A8 要回答的第一个问题
（社区文档说 ~ 包 344B，本仓库注释说 332B，差 12 字节）。

用法
----
    # ① 先停掉 recorder！否则两个心跳发送者会打架（见下）
    docker compose stop gt7-recorder

    # ② 抓包（心跳字符决定 PS5 回哪种包）
    python tools/capture_raw.py --heartbeat ~ --seconds 120
    python tools/capture_raw.py --heartbeat B --seconds 120
    python tools/capture_raw.py --heartbeat C --seconds 120
    python tools/capture_raw.py --heartbeat A --seconds 120     # 对照组

    # ③ 恢复 recorder
    docker compose start gt7-recorder

🔴 **必须先停 recorder**：GT7 只对「发心跳的那个 UDP 源」回包，两个进程同时
   发心跳会让 PS5 收到两份，包序混乱、长度分布也看不准。工具启动时会检测
   33739 端口有没有别的进程在监听，有就拒绝启动。

🔴 **包类型在「进入赛道」那一刻确定，中途改心跳完全无效**（实测，GT7 1.72）：

   · 在赛道里跑着，依次换 A → ~ → B → C 各抓 90 秒
     → **四段全是 296 字节的 A 包**，心跳字符等于没发
   · 先发 `~` 心跳、再让玩家**退出赛道重新进入**
     → 立刻变成 344 字节的 `~` 包

   所以测每种包类型都要走一遍「退出 → 重进」。光换心跳是白费功夫。

输出
----
默认写到 `capture_<心跳>_<时间>.jsonl`，每行一个包：

    {"t": 12.34, "n": 368, "enc": "a1b2...", "dec": "9f8e..." | null}

`dec` 为 null 表示解不开（密钥不对/不是遥测包）——这种包也会存下来，
因为"收到了但解不开"本身就是信息（比如固件换了加密）。

判读
----
结束时打印长度分布。三种典型结果：

  · 全是 296        → PS5 没按请求的包类型发（心跳没生效，或固件不支持）
  · 出现 332 / 344  → ~ 包的真实长度（**实测 GT7 1.72 = 344**，本仓库
                      早先注释写 332 是错的）
  · 出现 368        → C 包可用，A7 有数据了
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from probe_fields import PORT_ADV, PORT_HB, decrypt_packet   # noqa: E402


def _port_busy(port: int) -> bool:
    """检测端口是否已被别的进程监听（Linux 上能查出来，Windows 查不到）。"""
    if sys.platform == "win32":
        return False        # Windows 的 SO_REUSEADDR 语义不同，交给用户自己确认
    import subprocess
    try:
        out = subprocess.run(["ss", "-lun"], capture_output=True,
                             text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f":{port}" in out


def capture(heartbeat: str, seconds: float, out_path: Path,
            ps5: str | None, verbose: bool) -> int:
    if _port_busy(PORT_HB):
        print(f"✗ UDP {PORT_HB} 已被占用 —— recorder 还在跑？\n"
              f"  先停掉它：docker compose stop gt7-recorder\n"
              f"  （不停的话两个心跳发送者会打架，长度分布看不准）")
        return 2

    socks = []
    for p in (PORT_HB, PORT_ADV):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("0.0.0.0", p))
            # 🔴 非阻塞 + 内层 while：早先用 settimeout(0.2) 且每轮只收一个包，
            #    结果每轮最多收 2 个（每个 socket 一个）就要等 0.4 秒超时 ——
            #    实测 25 秒只收到 148 个包，而 60Hz 应该是 1500 个。
            #    采样率掉到 6Hz 会让"验证偏移"这类统计完全不可靠。
            s.setblocking(False)
            socks.append(s)
        except OSError as e:
            print(f"  ! 无法绑定 {p}: {e}")
    if not socks:
        print("✗ 一个端口都绑不上，无法抓包")
        return 2

    targets = [(ps5, PORT_HB)] if ps5 else []
    targets.append(("255.255.255.255", PORT_HB))
    hb = heartbeat.encode("ascii")

    lens: Counter = Counter()
    srcs: Counter = Counter()        # 所有来源（含噪音）
    gt7_srcs: Counter = Counter()    # **能解开的**来源 = 真正的 PS5
    n_ok = n_bad = 0
    t0 = time.time()
    last_hb = 0.0
    out = out_path.open("w", encoding="utf-8")
    print(f"\n心跳 = {heartbeat!r}，抓 {seconds:.0f} 秒 → {out_path}")
    print("（请在赛道里开，带几个大弯 + 一段直道；菜单/停车场不算）")
    try:
        while time.time() - t0 < seconds:
            now = time.time()
            if now - last_hb > 1.0:
                for tgt in targets:
                    try:
                        socks[0].sendto(hb, tgt)
                    except OSError:
                        pass
                last_hb = now
            got = False
            for s in socks:
                # 一次把缓冲里积压的包**全收完**，再让出 CPU
                while True:
                    try:
                        data, _addr = s.recvfrom(4096)
                    except (BlockingIOError, socket.timeout, OSError):
                        break
                    got = True
                    n = len(data)
                    lens[n] += 1
                    dec = decrypt_packet(data)
                    src = f"{_addr[0]}:{_addr[1]}"
                    srcs[src] += 1
                    if dec is None:
                        n_bad += 1
                    else:
                        n_ok += 1
                        gt7_srcs[src] += 1
                    out.write(json.dumps({
                        "t": round(now - t0, 3),
                        "n": n,
                        "src": src,
                        "enc": data.hex(),
                        "dec": dec.hex() if dec is not None else None,
                    }, separators=(",", ":")) + "\n")
                    if verbose:
                        print(f"  [{now - t0:6.2f}s] {n:3d}B "
                              f"{'解密 OK' if dec is not None else '解不开'}")
            # 实时状态（同一行刷新）
            top = " ".join(f"{k}B×{v}" for k, v in lens.most_common(3))
            sys.stdout.write(f"\r  {n_ok + n_bad:5d} 包 / {now - t0:5.1f}s   {top}   ")
            sys.stdout.flush()
            if not got:
                time.sleep(0.002)
    finally:
        out.close()
        for s in socks:
            s.close()

    print("\n\n=== 长度分布 ===")
    if not lens:
        print("  一个包都没收到。检查：")
        print("   · PS5 是否在同一网段、GT7 的『遥测上传』是否打开")
        print("   · 是否已经进到赛道里（菜单态不发遥测）")
        return 1
    for n, c in lens.most_common():
        note = {296: "格式 A（基础包）", 316: "格式 B（+运动数据）",
                332: "~ 扩展包（本仓库注释）",
                344: "~ 扩展包（社区文档）",
                368: "格式 C（GT7 1.68+）"}.get(n, "未知长度")
        print(f"  {n:4d} B  ×{c:6d}   {note}")
    print(f"\n  解密成功 {n_ok} / 失败 {n_bad}")
    if srcs:
        print("\n=== 来源分布 ===")
        for src, c in srcs.most_common():
            mark = " ← GT7（能解开）" if gt7_srcs.get(src) else " （不是 GT7：解不开）"
            print(f"  {src:24s} ×{c:6d}{mark}")
        if gt7_srcs:
            print(f"\n  ✅ PS5 地址 = {gt7_srcs.most_common(1)[0][0].split(':')[0]}"
                  f"（下次可用 --ps5 指定它，更稳）")
        else:
            print("\n  ⚠️ 没有任何一个来源能解开 —— 收到的都是局域网噪音。\n"
                  "     检查 PS5 是否真的进了赛道（菜单/停车场不发遥测）。")
    if n_ok + n_bad == 0:
        return 1
    if n_bad and not n_ok:
        print("  ⚠️ 全部解不开 —— 可能固件换了密钥（A4 项，目前无解）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="GT7 原始遥测抓包器（第 4 批：A8 / A3 / A7）")
    ap.add_argument("--heartbeat", default="A", choices=["A", "B", "~", "C"],
                    help="心跳字符，决定 PS5 回哪种包（默认 A）")
    ap.add_argument("--seconds", type=float, default=120.0, help="抓包秒数")
    ap.add_argument("--out", default=None, help="输出文件（默认自动命名）")
    ap.add_argument("--ps5", default=None,
                    help="PS5 的 IP（不填则只靠广播；填了更稳）")
    ap.add_argument("-v", "--verbose", action="store_true", help="逐包打印")
    a = ap.parse_args()

    hb_tag = {"~": "tilde", "A": "A", "B": "B", "C": "C"}[a.heartbeat]
    out = Path(a.out) if a.out else Path(
        f"capture_{hb_tag}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")
    return capture(a.heartbeat, a.seconds, out, a.ps5, a.verbose)


if __name__ == "__main__":
    sys.exit(main())
