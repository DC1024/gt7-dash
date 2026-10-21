#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 Dockerfile 的「自检失败 ⇒ 构建失败」这道闸是真的有效。

这是 P1-3 最容易被自欺的一环。改完之后**看着**没毛病：
    RUN chmod 755 /app/gt7-decrypt && /app/gt7-decrypt -selftest
但"构建会因此失败"这句话，只有在**故意喂一个坏二进制**试一次之后才算数。
否则很可能跟原来一样：以为加了闸，实际那条 RUN 被 Docker 层缓存跳过了、
或者后面还有 `|| true`、或者 `&&` / `||` 优先级搞错。

三个用例（全程在服务器临时目录，不动 /opt/gt7-recorder）：
  A. 坏二进制（IV 对调，自检必失败）+ 新写法  → 期望 **构建失败**
  B. 坏二进制 + 旧 `|| echo` 写法             → 期望 **构建成功**（证明旧写法是假闸）
  C. 好二进制 + 新写法                        → 期望 **构建成功**（证明闸不误伤）

用法：
    GT7_SERVER_PASSWORD=xxx python tools/verify_dockerfile_gate.py
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
from pathlib import Path

import paramiko

REPO = Path(__file__).resolve().parent.parent
HOST = "192.168.43.18"
USER = "root"
WORK = "/tmp/gt7-gate-test"

GOOD_BIN = REPO / "gt7-decrypt" / "gt7-decrypt-linux-amd64"
# 🔴 坏二进制的路径必须用环境变量给：Git Bash 的 /tmp 是 msys 的虚拟路径
#    （C:\Users\...\AppData\Local\Temp），Python 的 Path("/tmp") 解析成
#    当前盘符根的 \tmp —— 两者根本不是同一个目录，写死必然找不到。
BROKEN_BIN = Path(os.environ.get("GT7_BROKEN_BIN", "")) if os.environ.get("GT7_BROKEN_BIN") else None

# 最小化片段：只留"装二进制 + 跑自检"这一层。
# 不复制真 Dockerfile 全文，因为它的 apt-get 要联网、构建几分钟；
# 而这道闸就只是那一行 RUN，单独测它足够且快得多。
DFL_NEW = """\
FROM python:3.12-slim
WORKDIR /app
COPY gt7-decrypt /app/gt7-decrypt
RUN chmod 755 /app/gt7-decrypt && /app/gt7-decrypt -selftest
"""

# 改造前的写法：自检失败只打印一行提示，构建照过
DFL_OLD = """\
FROM python:3.12-slim
WORKDIR /app
COPY gt7-decrypt /app/gt7-decrypt
RUN chmod 755 /app/gt7-decrypt && /app/gt7-decrypt -selftest || echo "(自检提示：真实包解密已在部署时验证过)"
"""


def main() -> int:
    pwd = os.environ.get("GT7_SERVER_PASSWORD")
    if not pwd:
        print("ERROR: 需要 GT7_SERVER_PASSWORD")
        return 2
    if BROKEN_BIN is None:
        print("ERROR: 需要 GT7_BROKEN_BIN 指向那个自检会失败的 Linux 二进制")
        return 2
    for p in (GOOD_BIN, BROKEN_BIN):
        if not p.exists():
            print(f"ERROR: 找不到 {p}")
            return 2

    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    cli.connect(HOST, username=USER, password=pwd, timeout=20)

    def run(cmd: str, timeout: int = 900) -> tuple[int, str]:
        _in, out, err = cli.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode("utf-8", "replace") + err.read().decode("utf-8", "replace")

    run(f"rm -rf {WORK}")

    sftp = cli.open_sftp()
    for tag, path in (("good", GOOD_BIN), ("broken", BROKEN_BIN)):
        d = f"{WORK}/{tag}"
        run(f"mkdir -p {d}")
        sftp.put(str(path), f"{d}/gt7-decrypt")
        sftp.putfo(io.BytesIO(DFL_NEW.encode()), f"{d}/Dockerfile.new")
        sftp.putfo(io.BytesIO(DFL_OLD.encode()), f"{d}/Dockerfile.old")
    sftp.close()

    print("=== 传输校验 ===")
    for tag, path in (("good", GOOD_BIN), ("broken", BROKEN_BIN)):
        _rc, so = run(f"md5sum {WORK}/{tag}/gt7-decrypt")
        lm = hashlib.md5(path.read_bytes()).hexdigest()
        rm = so.split()[0] if so.strip() else ""
        ok = "✓" if lm == rm else "✗"
        print(f"  [{tag:<6}] local={lm} remote={rm} {ok}")
        if lm != rm:
            cli.close()
            return 1

    cases = [
        ("A  坏二进制 + 新写法      （期望：构建失败）", "broken", DFL_NEW, False),
        ("B  坏二进制 + 旧 || echo （期望：构建成功=假闸）", "broken", DFL_OLD, True),
        ("C  好二进制 + 新写法      （期望：构建成功）", "good", DFL_NEW, True),
    ]

    results = []
    for i, (title, tag, dfl_body, expect_ok) in enumerate(cases):
        print(f"\n{'=' * 62}\n{title}\n{'=' * 62}")
        d = f"{WORK}/{tag}"
        # 每个用例用独立的 Dockerfile 文件名，避免同名文件被上一层缓存带偏；
        # --no-cache 确保 RUN 真的执行（缓存只存成功层，但显式关掉更省心）。
        name = f"Dockerfile.case{i}"
        sftp = cli.open_sftp()
        sftp.putfo(io.BytesIO(dfl_body.encode()), f"{d}/{name}")
        sftp.close()

        rc, out = run(
            f"cd {d} && docker build --no-cache -f {name} -t gt7-gate-c{i} . "
            f">/tmp/gate{i}.log 2>&1; echo DOCKER_RC=$?"
        )
        rcline = [l for l in out.splitlines() if l.startswith("DOCKER_RC=")]
        docker_rc = int(rcline[0].split("=")[1]) if rcline else -1
        built_ok = docker_rc == 0
        verdict = built_ok == expect_ok
        print(f"  docker build 退出码 = {docker_rc} → 构建{'成功' if built_ok else '失败'}")
        print(f"  符合预期: {'✓' if verdict else '✗ 与预期不符'}")
        if not built_ok:
            _rc, tail = run(f"grep -E 'SELFTEST|ERROR|error|exit code' /tmp/gate{i}.log | tail -6")
            print("  构建失败的关键行：")
            for l in tail.strip().splitlines():
                print("     ", l)
        results.append((title, verdict))

    run(f"docker rmi $(docker images -q 'gt7-gate-*') >/dev/null 2>&1; rm -rf {WORK}")
    cli.close()

    print("\n" + "=" * 62)
    all_ok = True
    for title, verdict in results:
        print(f"{'PASS' if verdict else 'FAIL'}  {title}")
        all_ok = all_ok and verdict
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
