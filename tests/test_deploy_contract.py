# -*- coding: utf-8 -*-
"""G8：部署必须来自仓库，而不是「谁手上的工作区」。

这一组测试守的是**流程约束**，不是功能。它们挡不住代码 bug，但能挡住
那类"线上和仓库不是一回事、而且没人发现"的事故——这类事故排查成本最高，
因为所有常规手段（读代码、跑测试、看日志）看到的都是另一个版本。

历史事实（都是真踩过的）：
  · 服务器 `/opt` 是 scp 过来的工作区副本，没有 git，无法回答"线上是哪个版本"；
  · Windows 工作区是 CRLF，传过去之后 `.sh` 报 `bad interpreter: /bin/sh^M`；
  · 镜像靠服务器上手写的叠加 Dockerfile 构建，那份文件不在仓库里；
  · `docker compose up --force-recreate gt7-dashboard` 因为 depends_on
    连带重建了 recorder，把正在跑的比赛切成两个文件。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class TestDeployScript:
    def test_脚本存在(self):
        assert (ROOT / "tools" / "deploy.sh").is_file(), (
            "没有部署脚本 → 又回到「手工 scp」那条路")

    def test_有_ghcr_拉镜像模式(self):
        text = _read("tools/deploy.sh")
        assert "docker pull" in text, (
            "缺少 ghcr 拉取路径：线上应该跑 CI 构建出来的那个镜像，"
            "而不是在服务器上另构建一次")
        assert "ghcr.io/dc1024/gt7-dash" in text

    def test_拉完镜像才重启(self):
        """不 pull 直接 up，compose 会静默沿用本地旧镜像。

        这是最隐蔽的一类"部署成功"：容器起来了、健康检查也绿，
        但跑的其实是上一版。
        """
        text = _read("tools/deploy.sh")
        pull = text.find("docker pull")
        up = text.find("compose up")
        assert 0 < pull < up, "docker pull 必须出现在 compose up 之前"

    def test_用_git_archive_而不是拷工作区(self):
        text = _read("tools/deploy.sh")
        assert "git archive" in text, (
            "本地构建路径必须用 `git archive`：只有它能保证"
            "「传上去的字节 == 索引里的字节」")
        bad = [l for l in text.splitlines()
               if not l.strip().startswith("#")
               and re.search(r"\b(scp\s+-r|rsync\s+-a\s+\.|tar\s+-?c?zf?\s+\S+\s+\.)",
                             l)]
        assert not bad, f"deploy.sh 里还有整目录拷贝：{bad}"

    def test_拒绝脏工作区(self):
        text = _read("tools/deploy.sh")
        assert "git status --porcelain" in text, (
            "没有脏工作区检查 —— 未提交的改动传上去，线上就跑着"
            "一个不存在于任何 commit 的版本")

    def test_重启用_no_deps(self):
        text = _read("tools/deploy.sh")
        assert "--no-deps" in text, (
            "不带 --no-deps 会连带重建 recorder，正在跑的比赛被切成两个文件")

    def test_部署后校验线上版本(self):
        text = _read("tools/deploy.sh")
        assert "/api/health" in text, (
            "部署完不校验 /api/health 的 version，等于不知道到底生效没有")
        assert "gt7_version" in text, "版本号必须从唯一真源取"

    def test_检查解包后的换行(self):
        text = _read("tools/deploy.sh")
        assert "\\r" in text or "CRLF" in text, (
            "没有换行复核 —— CRLF 的 .sh 在 Linux 上会 bad interpreter")


class TestDeployRootUnified:
    """T3：两个运维入口必须指向同一个目录。

    历史上 deploy.sh 用 /opt/gt7-dash、manage.sh 用 /opt/gt7-recorder，
    结果 `manage.sh status` 看的是另一个容器。
    """

    def _root_of(self, text: str, pattern: str) -> str:
        m = re.search(pattern, text, re.M)      # 要 re.M：两条定义都在行首
        assert m, f"找不到部署根目录定义（pattern={pattern}）"
        return m.group(1)

    def test_两个脚本根目录一致(self):
        d = self._root_of(_read("tools/deploy.sh"), r'GT7_ROOT:-([^}"]+)')
        m = self._root_of(_read("manage.sh"), r'^DIR="([^"]+)"')
        assert d.strip() == m.strip(), (
            f"deploy.sh 用 {d}、manage.sh 用 {m} —— 两个运维入口指向不同目录")


class TestLineEndings:
    def test_gitattributes_默认强制_lf(self):
        text = _read(".gitattributes")
        assert re.search(r"^\*\s+text=auto\s+eol=lf", text, re.M), (
            "缺少 `* text=auto eol=lf`：Windows 检出会把 .py/.sh 变成 CRLF，"
            "scp 部署时一起带过去")

    def test_bat_仍然用_crlf(self):
        text = _read(".gitattributes")
        assert re.search(r"^\*\.bat\s+text\s+eol=crlf", text, re.M), (
            "把 .bat 也改成 LF 会让 cmd 解析异常")

    def test_索引里没有_crlf_文件(self):
        """`git ls-files --eol` 的 i/ 列必须是 lf。

        工作区可以是 crlf（Windows 正常现象），但**索引**必须干净 ——
        `git archive` 导的是索引，索引干净快照就干净。
        """
        import subprocess
        r = subprocess.run(["git", "ls-files", "--eol"], cwd=ROOT,
                           capture_output=True, text=True)
        if r.returncode != 0:
            pytest.skip("不是 git 仓库或没有 git 命令")
        bad = []
        for line in r.stdout.splitlines():
            m = re.match(r"^i/(\S+)\s+w/(\S+)\s+\S+\s+(.+)$", line)
            if not m:
                continue
            idx, _, path = m.groups()
            if idx == "crlf" and not path.endswith((".bat", ".cmd")):
                bad.append(path)
        assert not bad, f"索引里有 CRLF 文件（会原样进部署快照）：{bad[:10]}"


class TestDockerfileIsSelfContained:
    def test_基镜像可通过构建参数替换(self):
        text = _read("Dockerfile")
        assert re.search(r"^ARG BASE_IMAGE=", text, re.M), (
            "没有 BASE_IMAGE 参数 → 无外网环境又得在服务器上手写叠加 Dockerfile")
        assert re.search(r"^FROM \$\{BASE_IMAGE\}", text, re.M), (
            "ARG 声明了但 FROM 没用它")

    def test_apt_可跳过(self):
        text = _read("Dockerfile")
        assert "ARG SKIP_APT" in text, (
            "没有 SKIP_APT → 基镜像已含 tini 时仍会跑 apt-get update，"
            "内网服务器上直接构建失败")

    def test_自检仍然是无条件的(self):
        # 这条与 test_release_gates 里的门禁重复，但角度不同：
        # 那边查"有没有吞错误"，这边查"改成多阶段/多分支后有没有被跳过"。
        text = _read("Dockerfile")
        assert "-selftest" in text
        assert "gt7-decrypt/gt7-decrypt-linux-amd64" in text, (
            "解密器二进制没被拷进镜像")


class TestCompose:
    def test_没有_depends_on(self):
        text = _read("docker-compose.yml")
        # 只看指令行，不看注释里对 depends_on 的解释
        hits = [l for l in text.splitlines()
                if l.strip().startswith("depends_on:")]
        assert not hits, (
            "depends_on 会让「重建 dashboard」连带重建 recorder —— "
            "正在跑的比赛被切成两个文件（真实丢过 30 秒）")

    def test_镜像名可覆盖(self):
        text = _read("docker-compose.yml")
        assert "${GT7_IMAGE:-" in text, (
            "compose 写死 ghcr 镜像名 → 本地构建的 tag 永远不会被用上")


class TestVersionStampedIntoImage:
    def test_deploy_构建的镜像带版本_tag(self):
        text = _read("tools/deploy.sh")
        assert ":v$VERSION" in text, (
            "镜像不打版本 tag，线上跑的是哪个版本又变成谜")
