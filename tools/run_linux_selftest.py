#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把交叉编译出的 gt7-decrypt-linux-amd64 送到服务器上实跑 -selftest。

为什么必须实跑 ELF，不能只靠本机 `go build` 返回 0：
  · 本机是 Windows，跑不了 Linux ELF。`go build` 只证明"编译通过"，
    不证明这个**发布物**里的自检真的会 OK；
  · 也证明不了新向量确实被编进了这个二进制（Go 把小常量编成字节数组，
    `strings` 抓不到 —— 这条检查不可靠，别再用）；
  · 最要紧的是：Dockerfile 里 COPY 的是这个预编译文件。如果它不是由
    新 main.go 编的，容器里跑的仍是那个"自证循环"自检，改了半天没生效。

用法（密码只从环境变量读，不落盘）：
    GT7_SERVER_PASSWORD=xxx python tools/run_linux_selftest.py
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parent.parent
HOST = "192.168.43.18"
USER = "root"
LOCAL_BIN = REPO / "gt7-decrypt" / "gt7-decrypt-linux-amd64"
REMOTE_DIR = "/tmp/gt7-selftest"


def main() -> int:
    pwd = os.environ.get("GT7_SERVER_PASSWORD")
    if not pwd:
        print("ERROR: 需要 GT7_SERVER_PASSWORD 环境变量")
        return 2
    if not LOCAL_BIN.exists():
        print(f"ERROR: 找不到 {LOCAL_BIN}")
        return 2

    data = LOCAL_BIN.read_bytes()
    local_md5 = hashlib.md5(data).hexdigest()
    print(f"[local]  {LOCAL_BIN}")
    print(f"[local]  size={len(data)} md5={local_md5}")

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=pwd, timeout=20)

    def run(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
        _in, out, err = cli.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode("utf-8", "replace"), err.read().decode("utf-8", "replace")

    # 🔴 用完必须清掉：上一次漏了 finally，服务器 /tmp/gt7-selftest 留了 1.6MB 的
    #    旧二进制，下次改完二进制再跑，很容易误以为"上传的就是新的"。
    try:
        return _run_checks(cli, run, local_md5)
    finally:
        try:
            run(f"rm -rf {REMOTE_DIR}", timeout=20)
            print(f"\n[cleanup] 已删除远端 {REMOTE_DIR}")
        except Exception as exc:  # 清理失败不该掩盖真正的测试结论
            print(f"\n[cleanup] 删除 {REMOTE_DIR} 失败（可手动清理）: {exc}")
        cli.close()


def _run_checks(cli, run, local_md5: str) -> int:
    rc, _, _ = run(f"rm -rf {REMOTE_DIR} && mkdir -p {REMOTE_DIR}")
    if rc != 0:
        print("ERROR: 建临时目录失败")
        return 1

    sftp = cli.open_sftp()
    remote_bin = f"{REMOTE_DIR}/gt7-decrypt"
    sftp.put(str(LOCAL_BIN), remote_bin)
    sftp.close()

    rc, so, _ = run(f"md5sum {remote_bin}")
    remote_md5 = so.split()[0] if so.strip() else ""
    print(f"[remote] md5={remote_md5}")
    if remote_md5 != local_md5:
        print(f"FAIL: md5 不一致 local={local_md5} remote={remote_md5}")
        return 1

    run(f"chmod 755 {remote_bin}")

    print("\n=== 1) -selftest ===")
    rc, so, se = run(f"{remote_bin} -selftest")
    print(f"rc={rc}")
    print(f"stdout: {so.strip()}")
    if se.strip():
        print(f"stderr: {se.strip()}")
    selftest_ok = rc == 0 and "SELFTEST OK" in so

    # 2) 真实路径：喂一条完整 296 字节密文，确认 stdin 模式也能解出同一份明文。
    #    这条才是"解密器真的能干活"的证据；-selftest 只测了内置向量那一条。
    print("\n=== 2) stdin 模式解一条完整 296 字节包 ===")
    sys.path.insert(0, str(REPO / "tools"))
    from probe_fields import encrypt_packet_pair  # noqa: E402
    from verify_salsa20 import build_plain       # noqa: E402

    cipher, plain = encrypt_packet_pair(build_plain())
    assert len(cipher) == 296, f"期望 296 字节，实际 {len(cipher)}"

    cmd = (f"printf '%s\\n' {cipher.hex()} | {remote_bin}")
    rc, so, se = run(cmd)
    got = so.strip()
    want = plain.hex()
    pipe_ok = rc == 0 and got == want
    print(f"rc={rc}  输出长度={len(got)}")
    if pipe_ok:
        print("明文逐字节一致 ✓")
    else:
        print(f"  期望前 64: {want[:64]}")
        print(f"  实际前 64: {got[:64]}")
        if se.strip():
            print(f"  stderr: {se.strip()[:300]}")

    print("\n" + "=" * 50)
    print(f"-selftest        : {'PASS' if selftest_ok else 'FAIL'}")
    print(f"stdin 解密完整包 : {'PASS' if pipe_ok else 'FAIL'}")
    return 0 if (selftest_ok and pipe_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
