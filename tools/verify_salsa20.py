#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对拍：tools/probe_fields.py 里的**纯 Python Salsa20** vs 生产用的 Go 解密器。

为什么必须有这个
----------------
gt7-decrypt/main.go 的注释写着「手写 Salsa20 实现极易出错（我试过两次都
算不出正确密钥流）」。所以探针里那份 Python 实现不能"看着对"就算数，
必须拿 Go 的、每天都在解密真实数据的实现来逐字节校验。

做法
----
1. 造一个 296 字节的明文包（magic + 一堆非零字节，方便看出错位）；
2. 用 Python 加密成密文（encrypt_packet 会处理好"nonce 取自密文"的循环依赖）；
3. 交给 Go 解密器解，比对结果是否等于第 1 步的明文。

用法
----
    # 有 Go 二进制（本机编译过 / 服务器上）
    python tools/verify_salsa20.py --go ./gt7-decrypt.exe

    # 没有就先导出，拿到能跑 Go 二进制的机器上再对
    python tools/verify_salsa20.py --emit _salsa_check
    # → 生成 cipher.hex（喂给 Go 的 stdin）和 plain.hex（期望输出）
    #   ./gt7-decrypt < cipher.hex > got.hex && diff got.hex plain.hex
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_fields import (  # noqa: E402
    decrypt_packet, encrypt_packet_pair,
)


def build_plain(seed: int = 20261009) -> bytes:
    """造一个非零、无周期规律的明文包（全零的话错了也看不出来）。"""
    buf = bytearray(296)
    buf[0:4] = (0x47375330).to_bytes(4, "little")
    x = seed
    for i in range(4, 296):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        buf[i] = (x >> 16) & 0xFF
    buf[0x40:0x44] = b"\x00\x00\x00\x00"   # 稍后由 encrypt_packet 覆盖
    return bytes(buf)


def main() -> int:
    p = argparse.ArgumentParser(description="Python Salsa20 vs Go 解密器 对拍")
    p.add_argument("--go", default=None, help="gt7-decrypt 二进制路径（直接对拍）")
    p.add_argument("--emit", default=None, help="导出 cipher.hex / plain.hex 的目录")
    args = p.parse_args()

    plain = build_plain()
    cipher, plain_actual = encrypt_packet_pair(plain)

    # 自检第一步：Python 自己能解回自己（这只证明自洽，不证明对）
    back = decrypt_packet(cipher)
    if back != plain_actual:
        print("[FAIL] Python 自己都解不回自己")
        return 1
    print("[OK]   Python 自洽（能解回自己加密的包）")

    if args.emit:
        d = Path(args.emit)
        d.mkdir(parents=True, exist_ok=True)
        # ⚠️ newline="\n"：默认文本模式在 Windows 上会把 \n 写成 \r\n，
        #    拿到 Linux 上跟 Go 的输出 cmp 会"差一个字节"，查半天是换行。
        (d / "cipher.hex").write_text(cipher.hex() + "\n", encoding="utf-8", newline="\n")
        (d / "plain.hex").write_text(plain_actual.hex() + "\n",
                                     encoding="utf-8", newline="\n")
        print(f"[OK]   已导出 {d/'cipher.hex'} 与 {d/'plain.hex'}")
        print("       在能跑 Go 二进制的机器上：")
        print("         ./gt7-decrypt < cipher.hex > got.hex")
        print("         diff got.hex plain.hex && echo 一致")
        return 0

    if args.go:
        r = subprocess.run([args.go], input=cipher.hex() + "\n",
                           capture_output=True, text=True, timeout=60)
        got = r.stdout.strip()
        if not got or got == "FAIL":
            print(f"[FAIL] Go 解不开（rc={r.returncode}）stderr={r.stderr.strip()[:200]}")
            return 1
        if got != plain_actual.hex():
            print("[FAIL] Go 解出来的和期望明文不一致 —— Python Salsa20 写错了")
            print("  got :", got[:80])
            print("  want:", plain_actual.hex()[:80])
            return 1
        print("[OK]   Go 解密器解出来的 == Python 的明文 —— 逐字节一致")
        return 0

    print("需要 --go <二进制> 或 --emit <目录>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
