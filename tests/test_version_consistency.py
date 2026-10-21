# -*- coding: utf-8 -*-
"""G3：版本号只有一个来源，且所有出口都跟它一致。

背景：仓库里曾经出现 `APP_VERSION = "1.2.0"` 而 git 已经到 `v1.3.4` ——
用户报 bug 时给的版本号是假的，日志、`/api/health`、exe 文件名三处各不相同，
定位问题第一步就卡住。

现在的约定：
  · `gt7_version.py` 是唯一真源；
  · 仪表盘 / 接收器 `from gt7_version import __version__`；
  · Go 解密器由 CI 用 `-ldflags -X main.version=` 注入同一个值；
  · tag 必须与 `__version__` 相同（发版时 CI 会校验，本地也查）。

这里的测试分两类：
  1. **静态**：源码里不许再出现写死的版本字面量、Dockerfile 必须带上
     `gt7_version.py`（漏了就是「镜像里 import 失败 → 降级成 0.0.0」）；
  2. **运行时**：真正 import 两个模块，比对它们对外暴露的版本。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")

# 这两个文件的文件名带连字符，不能直接 import，测试里用 conftest 的 loader
SOURCES = ["gt7-dashboard.py", "gt7-recorder.py"]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _real_version() -> str:
    """直接从 gt7_version.py 取，不 import —— 免得测试自己也被缓存影响。"""
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                  _read("gt7_version.py"), re.M)
    assert m, "gt7_version.py 里找不到 __version__ 赋值"
    return m.group(1)


# ------------------------------------------------------------------ 真源本身

class TestVersionSource:
    def test_版本号是_semver(self):
        v = _real_version()
        assert SEMVER.match(v), f"版本号 {v!r} 不是 x.y.z 形式"

    def test_能被_import(self):
        import gt7_version
        assert gt7_version.__version__ == _real_version()

    def test_tag_与版本号一致(self):
        """CI 在 tag 上跑测试时，tag 必须等于 `__version__`。

        为什么放在测试里而不是 workflow 的 shell 步骤：本地 `git tag` 打错
        是同一类事故，测试能同时在两边拦住。非 tag 环境自动跳过。
        """
        ref = os.environ.get("GITHUB_REF", "")
        if not ref.startswith("refs/tags/"):
            pytest.skip("不在 tag 构建上")
        tag = ref[len("refs/tags/"):]
        assert tag == "v" + _real_version(), (
            f"tag {tag} 与 gt7_version.__version__=v{_real_version()} 不一致 —— "
            "发出去的包会自称另一个版本")


# ------------------------------------------------------------ 源码不许写死

class TestNoHardcodedVersion:
    @pytest.mark.parametrize("filename", SOURCES)
    def test_没有写死的_APP_VERSION_字面量(self, filename):
        """`APP_VERSION = "1.2.3"` 这种写法一旦回来，就等于又开了第二个真源。

        唯一放行 `"0.0.0"`：那是 `except ImportError` 分支里的兜底值，
        语义就是「不知道自己是哪版」，与写死一个真版本号是两回事。
        """
        text = _read(filename)
        bad = [v for v in re.findall(
            r'^\s*APP_VERSION\s*=\s*["\']([\d.]+)["\']', text, re.M)
            if v != "0.0.0"]
        assert not bad, (
            f"{filename} 里又有写死的版本号：{bad} —— "
            "改成 `from gt7_version import __version__ as APP_VERSION`")

    @pytest.mark.parametrize("filename", SOURCES)
    def test_确实从_gt7_version_导入(self, filename):
        text = _read(filename)
        assert "from gt7_version import __version__" in text, (
            f"{filename} 没有从唯一真源取版本")

    def test_Dockerfile_把版本文件一起拷进镜像(self):
        text = _read("Dockerfile")
        copy_lines = [l for l in text.splitlines()
                      if l.strip().upper().startswith("COPY")
                      and ".py" in l]
        assert any("gt7_version.py" in l for l in copy_lines), (
            "Dockerfile 的 COPY 没带上 gt7_version.py —— 镜像里 import 会失败，"
            "版本静默降级成 0.0.0，而且只有跑到 /api/health 才看得出来")

    def test_Go_侧留了可注入的_version_变量(self):
        text = _read("gt7-decrypt/main.go")
        assert re.search(r'^var version\s*=', text, re.M), (
            "main.go 需要 `var version = \"dev\"`，否则 CI 没法用 ldflags 注入")

    def test_发布工作流用_ldflags_注入_Go_版本(self):
        text = _read(".github/workflows/release-exe.yml")
        assert "-X main.version" in text, (
            "release-exe.yml 没给 Go 二进制注入版本，解出来的 gt7-decrypt 会报 dev")


# --------------------------------------------------------------- 运行时一致

class TestRuntimeAgreement:
    def test_仪表盘版本等于真源(self, dash):
        assert dash.APP_VERSION == _real_version(), (
            "仪表盘对外报的版本与 gt7_version.py 不一致")

    def test_接收器版本等于真源(self, rec):
        assert rec.APP_VERSION == _real_version(), (
            "接收器对外报的版本与 gt7_version.py 不一致")

    def test_两者相等(self, dash, rec):
        assert dash.APP_VERSION == rec.APP_VERSION

    def test_HTTP_Server_头带版本(self, dash):
        # `server_version` 是 Handler 的**类属性**（BaseHTTPRequestHandler 约定的
        # 覆盖点），不是模块级变量 —— 早先写成模块级会 AttributeError。
        v = dash.DashboardHandler.server_version
        assert _real_version() in v, (
            f"HTTP Server 头 {v!r} 里没有版本号 —— 抓包/代理日志里认不出跑的是哪版")

    def test_接收器_header_带版本(self, rec):
        # to_header() 在 Session 上（会话级元数据），不在 Recorder 上。
        h = rec.Session(session_id="s1", started_at=0.0).to_header()
        assert h.get("recorder_version") == _real_version(), (
            "录制文件头里没有正确的 recorder_version —— "
            "以后拿到一个 .jsonl.gz 无法判断是哪个版本录的")


class TestReadmeInSync:
    def test_README_写的版本与真源一致(self):
        text = _read("README.md")
        found = re.findall(r"v?(\d+\.\d+\.\d+)", text[:4000])
        assert found, "README 开头找不到版本号"
        assert _real_version() in found, (
            f"README 顶部版本号与 gt7_version.py ({_real_version()}) 不一致；"
            f"找到的是 {sorted(set(found))}")
