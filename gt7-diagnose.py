#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GT7 遥测连接诊断工具
=====================

回答一个问题：**为什么收不到 PS5 的遥测数据？**

分层排查，从最可能的原因开始：
  1. PS5 IP 是否正确 / 在线
  2. TCP 层能否连通
  3. UDP 广播是否到达本机
  4. 心跳能否唤醒 PS5

用法：
    python gt7-diagnose.py
    python gt7-diagnose.py --ps5 192.168.43.23
    python gt7-diagnose.py --ps5 auto        # 扫描网段找 PS5
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from concurrent.futures import ThreadPoolExecutor

PORT_HB = 33739      # 心跳 / 格式 A
PORT_ADV = 33740     # 格式 B/C（Addendum）

# 已知的 Sony OUI 段（B4:C2:E0 是 PS5/PS4 常见段）
SONY_HINTS = ("b4:c2:e0", "00:04:1f", "00:1a:64", "dc:cc:9e",
              "fc:fb:fb", "00:26:f2", "cc:32:e5")


def c_ok(s): print(f"  \033[32m[OK]\033[0m   {s}")
def c_bad(s): print(f"  \033[31m[FAIL]\033[0m {s}")
def c_warn(s): print(f"  \033[33m[WARN]\033[0m {s}")
def c_info(s): print(f"  [INFO] {s}")


# ---------------------------------------------------------------------------
# 1. 找到 PS5
# ---------------------------------------------------------------------------

def scan_for_ps5(timeout: float = 2.5) -> list[str]:
    """扫描网段，找出可能的主机（返回 IP 列表）。"""
    # 先判断本机网段
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        local_ip = probe.getsockname()[0]
    except OSError:
        local_ip = "192.168.43.96"
    finally:
        probe.close()

    subnet = ".".join(local_ip.split(".")[:3])
    print(f"\n\033[1m[1/5] 扫描网段 {subnet}.0/24 寻找 PS5\033[0m")

    def ping(i: int):
        ip = f"{subnet}.{i}"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.25)
        try:
            s.sendto(b"\x00" * 800, (ip, 9))   # UDP 触发 ARP
        except OSError:
            pass
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=64) as ex:
        list(ex.map(ping, range(1, 255)))
    time.sleep(timeout)

    # 读 ARP 表（Linux / Windows 分别处理）
    hosts: list[tuple[str, str]] = []
    if sys.platform.startswith("linux"):
        try:
            out = subprocess_out = __import__("subprocess").run(
                ["ip", "neigh", "show"], capture_output=True, timeout=5
            ).stdout.decode()
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 5 and p[0].startswith(subnet):
                    hosts.append((p[0], p[4]))
        except Exception:
            pass
    else:
        try:
            ps = (
                "Get-NetNeighbor -AddressFamily IPv4 | "
                f"Where-Object {{ $_.IPAddress -like '{subnet}.*' -and "
                "$_.State -ne 'Unreachable' }} | "
                "ForEach-Object { \"$($_.IPAddress)|$($_.LinkLayerAddress)\" }"
            )
            out = __import__("subprocess").run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=20,
            ).stdout.decode("utf-8", errors="replace")
            for line in out.splitlines():
                if "|" in line:
                    ip, mac = line.split("|")[:2]
                    ip, mac = ip.strip(), mac.strip()
                    if ip.startswith(subnet) and set(mac) != {"-"}:
                        hosts.append((ip, mac.replace("-", ":").lower()))
        except Exception:
            pass

    if not hosts:
        c_warn("未能扫描到任何主机")
        return []

    # 标出可能是 Sony 的
    sony = [h for h in hosts if any(h[1].startswith(o) for o in SONY_HINTS)]
    c_info(f"发现 {len(hosts)} 台在线设备")
    for ip, mac in sorted(hosts, key=lambda x: int(x[0].split(".")[-1])):
        mark = "  ← 可能是 PS5" if any(mac.startswith(o) for o in SONY_HINTS) else ""
        print(f"       {ip:16s} {mac}{mark}")

    if not sony:
        c_warn("没有发现 Sony OUI 的设备")
        print("       注意：这不是决定性证据，PS5 可能用随机 MAC（多数 PS5 支持手动设置）")

    return [ip for ip, _ in sony] or [ip for ip, _ in hosts]


# ---------------------------------------------------------------------------
# 2. 检查 PS5 连通性
# ---------------------------------------------------------------------------

def check_ping(ps5: str) -> bool:
    print(f"\n\033[1m[2/5] 检查 {ps5} 的网络连通性\033[0m")
    import subprocess
    try:
        if sys.platform.startswith("linux"):
            r = subprocess.run(["ping", "-c", "2", "-W", "1", ps5],
                               capture_output=True, timeout=10)
        else:
            r = subprocess.run(["ping", "-n", "2", "-w", "1000", ps5],
                               capture_output=True, timeout=10)
        alive = r.returncode == 0
    except Exception:
        alive = False

    if alive:
        c_ok(f"{ps5} 可以 ping 通")
    else:
        c_bad(f"{ps5} ping 不通")
        c_info("可能原因：")
        print("       · IP 记错了（PS5 设置→网络→查看连接状态）")
        print("       · PS5 用了访客 WiFi / 移动网络")
        print("       · PS5 在省电待机")
        print("       · 路由器开启了 AP 隔离")
    return alive


# ---------------------------------------------------------------------------
# 3. 监听广播
# ---------------------------------------------------------------------------

def listen_broadcast(seconds: int = 10.0) -> tuple[int, set]:
    print(f"\n\033[1m[3/5] 监听 {seconds:.0f} 秒 UDP 广播\033[0m")
    print(f"       监听端口 {PORT_HB} 和 {PORT_ADV}")
    print("       \033[33m请确保 PS5 现在正在赛道里开车\033[0m")

    socks = []
    for p in (PORT_HB, PORT_ADV):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            s.bind(("0.0.0.0", p))
            s.settimeout(0.5)
            socks.append(s)
        except OSError as e:
            c_warn(f"无法绑定端口 {p}: {e}")

    total = 0
    sources: set[str] = set()
    layouts: dict[int, int] = {}
    t0 = time.time()
    last_print = 0.0

    while time.time() - t0 < seconds:
        for s in socks:
            try:
                d, a = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                continue
            total += 1
            if a[0] != "255.255.255.255":
                sources.add(a[0])
            if total <= 3:
                print(f"       收到 {a[0]}: {len(d)} 字节")
            # 推测包格式
            if len(d) >= 400:
                layouts["C"] = layouts.get("C", 0) + 1
            elif len(d) >= 300:
                layouts["B"] = layouts.get("B", 0) + 1
            else:
                layouts["A"] = layouts.get("A", 0) + 1

        # 每秒打印一次进度
        if time.time() - last_print > 1.0:
            got = total
            sys.stdout.write(f"\r       {time.time()-t0:.0f}s...已收到 {got} 包   ")
            sys.stdout.flush()
            last_print = time.time()

    for s in socks:
        s.close()

    print()
    if total > 0:
        c_ok(f"收到 {total} 个包！来源: {sources if sources else '广播'}")
        if layouts:
            c_info(f"包格式分布: {layouts}（C=含车身坐标，A=基础）")
        c_info("链路正常 —— 你的接收器应该也能收到")
        return total, sources

    c_bad(f"{seconds:.0f} 秒内一个包都没收到")
    return 0, set()


# ---------------------------------------------------------------------------
# 4. 心跳唤醒
# ---------------------------------------------------------------------------

def try_heartbeat(ps5: str, seconds: int = 6.0) -> int:
    """主动向 PS5 发心跳，看它是否会开始广播。"""
    print(f"\n\033[1m[4/5] 向 {ps5} 发送心跳，尝试唤醒\033[0m")

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("0.0.0.0", PORT_HB))
    s.settimeout(1.0)

    got = 0
    t0 = time.time()
    last_hb = 0.0
    while time.time() - t0 < seconds:
        # 每秒发一次心跳
        if time.time() - last_hb > 1.0:
            for target in [(ps5, PORT_HB)]:
                try:
                    s.sendto(b"A", target)
                except OSError:
                    pass
            last_hb = time.time()

        try:
            d, a = s.recvfrom(2048)
            if len(d) > 1:      # 排除自己心跳的回环
                got += 1
                if got <= 3:
                    print(f"       收到 {a[0]}: {len(d)} 字节")
        except socket.timeout:
            pass
        except OSError:
            break
    s.close()

    if got:
        c_ok(f"心跳生效，收到 {got} 个包")
    else:
        c_warn("心跳后仍无响应")
        print("       如果 PS5 正在开车却收不到，问题多半在网络配置")
    return got


# ---------------------------------------------------------------------------
# 5. 结论
# ---------------------------------------------------------------------------

def print_conclusion(bcast_ok: bool, hb_ok: bool, ping_ok: bool) -> None:
    print(f"\n\033[1m[5/5] 诊断结论\033[0m")
    print("─" * 58)

    if bcast_ok:
        print("""
\033[32m● 网络层正常\033[0m —— 已经能收到 PS5 广播。

  如果接收器仍然收不到，检查：
    1. 接收器是否用 --network host 启动（bridge 收不到广播）
    2. 如果用了 --ps5 <ip>，IP 是否与 PS5 实际地址一致
    3. 数据目录权限（容器内 UID 10001）
""")
    else:
        print("""
\033[31m● 收不到任何包\033[0m —— 问题在网络侧，按顺序排查：

  \033[1m① PS5 遥测是否开启\033[0m（最常见原因）
     PS5 → 设置 → 网络 → [主机] → \033[33m「GT7 遥测」设为开启\033[0m
     这一步默认可能是关的，必须手动开。

  \033[2m② PS5 和本机是否在同一网段\033[0m
     两者 IP 必须是 192.168.43.x
     PS5 IP：设置 → 网络 → 查看连接状态

  \033[3m③ 是不是访客 WiFi\033[0m
     访客网络默认隔离设备，无法互发广播
     主机与 PS5 必须连同一个主网络

  \033[4m④ 路由器是否开了 AP 隔离\033[0m
     部分路由器有「访客网络隔离」「AP 隔离」选项

  \033[5m⑤ 防火墙是否拦截\033[0m
     Windows 防火墙可能拦 UDP 广播，试试关掉或加规则
""")
        if ping_ok:
            c_info("PS5 能 ping 通，说明 TCP 层没问题，纯粹是 UDP 广播被隔离")
            print("       → 大概率是 ③访客 WiFi 或 ④ AP 隔离")
        else:
            c_info("连 ping 都不通，先解决 IP 和网络连通性再说")


def main() -> int:
    p = argparse.ArgumentParser(description="GT7 遥测连接诊断")
    p.add_argument("--ps5", default=None,
                   help="PS5 的 IP。不填则自动扫描")
    p.add_argument("--listen", type=int, default=10,
                   help="监听广播的秒数，默认 10")
    p.add_argument("--skip-scan", action="store_true", help="跳过网段扫描")
    args = p.parse_args()

    print("=" * 60)
    print(" GT7 遥测连接诊断")
    print("=" * 60)

    ps5 = args.ps5
    if not ps5 or ps5.lower() == "auto":
        candidates = [] if args.skip_scan else scan_for_ps5()
        if candidates:
            ps5 = candidates[0]
            c_info(f"选择 {ps5} 作为疑似 PS5")
            print("       若选错了，用 --ps5 <正确IP> 重新指定")
        else:
            c_warn("未能自动定位 PS5，请手动指定 --ps5 <IP>")
            return 1
    print(f"\n目标 PS5: {ps5}")

    ping_ok = check_ping(ps5)
    n, _ = listen_broadcast(args.listen)
    bcast_ok = n > 0

    hb_ok = False
    if not bcast_ok:
        hb_ok = try_heartbeat(ps5, seconds=6.0) > 0

    print_conclusion(bcast_ok, hb_ok, ping_ok)
    return 0 if bcast_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(1)