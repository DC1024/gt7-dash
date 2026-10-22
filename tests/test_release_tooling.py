# -*- coding: utf-8 -*-
"""第 3 批（C2 / G2 / G4 / G7 / G9）的契约守卫。

这些改动有一个共同点：**失效时不会报错**。
  · CHANGELOG 落后于 git → 没人发现，直到有人去查某版改了什么；
  · 发行说明又变回手写 → Release 页写着十几个版本前的功能；
  · 基础镜像换回浮动 tag → 同一个 commit 两次构建出不同产物；
  · compose 掉了 logging 上限 → 几个月后磁盘满；
  · /metrics 字段被改名 → 告警规则静默失效（Prometheus 不会报错，
    只是查不到数据）。
所以逐条钉死。
"""
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class TestChangelog:
    def test_CHANGELOG_存在(self):
        assert (ROOT / "CHANGELOG.md").is_file(), (
            "没有 CHANGELOG —— 想知道某一版改了什么只能翻 git log")

    def test_CHANGELOG_与_git_历史一致(self):
        if not (ROOT / "CHANGELOG.md").is_file():
            # CHANGELOG 是 v1.4.2 才引入的。更早的 tag 上 CI 也会跑这条，
            # 那些提交里根本没有这个文件 —— 跳过，别让历史快照永远红。
            pytest.skip("该提交还没有 CHANGELOG.md（v1.4.2 之前）")
        """落后于 git 就失败，逼你跑一次生成脚本。

        这条断言的价值在于「不会悄悄落后」：手写 CHANGELOG 的结局一定是
        停在几个版本之前，而没有任何东西会提醒你。
        """
        import gen_changelog
        cur = _read("CHANGELOG.md")
        assert cur == gen_changelog.build(), (
            "CHANGELOG.md 落后于 git 历史，跑 `python tools/gen_changelog.py`")

    def test_每个发过的版本都有段落(self):
        """段落以 `gt7_version.py` 的版本变更为准，不是 tag。

        🔴 早先用 tag 判定，结果同一份文件在开发机（tag 多）和 CI（tag 少）
           上"期望内容"不同，测试永远红。版本号才是任何 clone 都一样的事实。
        """
        if not (ROOT / "CHANGELOG.md").is_file():
            pytest.skip("该提交还没有 CHANGELOG.md（v1.4.2 之前）")
        import re as _re
        import subprocess
        ver_shas = subprocess.run(
            ["git", "log", "--format=%H", "--", "gt7_version.py"],
            cwd=ROOT, capture_output=True, text=True).stdout.split()
        versions = set()
        for sha in ver_shas:
            out = subprocess.run(["git", "show", f"{sha}:gt7_version.py"],
                                 cwd=ROOT, capture_output=True, text=True)
            m = _re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']',
                           out.stdout, _re.M)
            if m:
                versions.add(m.group(1))
        text = _read("CHANGELOG.md")
        missing = sorted(v for v in versions if f"## v{v}" not in text)
        assert not missing, f"这些版本在 CHANGELOG 里没有段落：{missing}"


class TestReleaseNotesGenerated:
    def test_用_body_path_而不是内联正文(self):
        text = _read(".github/workflows/release-exe.yml")
        assert "body_path: release-notes.md" in text, (
            "发行说明又变回内联手写了 —— 手写的一定会落后")
        assert not re.search(r"^\s{10}body: \|", text, re.M), (
            "还留着内联 body 块，会和 body_path 冲突")

    def test_生成步骤调用了_changelog_脚本(self):
        text = _read(".github/workflows/release-exe.yml")
        assert "gen_changelog.py --version" in text, (
            "发行说明没从 CHANGELOG 取内容，等于又回到手写")

    def test_测试数仍是实时上报(self):
        text = _read(".github/workflows/release-exe.yml")
        assert "needs.test.outputs.passed" in text
        assert "373" not in text, "写死的用例数又回来了"


class TestEslintWiring:
    def test_有_eslint_配置(self):
        assert (ROOT / "eslint.config.mjs").is_file()

    def test_只开_no_undef_这类零噪声规则(self):
        text = _read("eslint.config.mjs")
        assert '"no-undef": "error"' in text
        # no-implicit-globals 会立刻产生 170 条噪声（页面架构就是顶层函数）
        assert '"no-implicit-globals"' not in text or "不开" in text

    def test_CI_有_js_lint_job(self):
        text = _read(".github/workflows/tests.yml")
        assert "js-lint:" in text
        assert "npx eslint _lint_js" in text

    def test_抽取脚本存在(self):
        assert (ROOT / "tools" / "lint_inline_js.py").is_file(), (
            "没有抽取脚本，eslint 就没有输入 —— 门禁是假的")


class TestImagePinned:
    def test_基础镜像按_digest_固定(self):
        text = _read("Dockerfile")
        m = re.search(r"^ARG BASE_IMAGE=(\S+)", text, re.M)
        assert m, "找不到 BASE_IMAGE"
        val = m.group(1)
        if val.startswith("python:"):
            assert "@sha256:" in val, (
                f"基础镜像还是浮动 tag（{val}）—— 同一个 commit 两次构建"
                "可能拿到不同基础镜像")


class TestLogRotation:
    def test_两个服务都有日志上限(self):
        text = _read("docker-compose.yml")
        assert text.count('max-size: "10m"') == 2, (
            "recorder 与 dashboard 都要设上限；漏一个就会把磁盘写满")
        assert text.count('max-file: "3"') == 2


class TestMetricsEndpoint:
    @pytest.fixture
    def metrics(self, dash, tmp_path):
        import threading
        from http.server import ThreadingHTTPServer
        import http.client
        from urllib.parse import urlparse

        (tmp_path / "_trash").mkdir()
        (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
        dash.HUB = dash.TelemetryHub(tmp_path / "status.json")
        s = ThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
        s.daemon_threads = True
        s.history_dir = str(tmp_path)   # type: ignore[attr-defined]
        s.allow_hosts = ()              # type: ignore[attr-defined]
        threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            u = urlparse(f"http://127.0.0.1:{s.server_address[1]}")
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
            c.request("GET", "/metrics")
            r = c.getresponse()
            body = r.read().decode("utf-8")
            c.close()
        finally:
            s.shutdown()
            s.server_close()
        return r.status, body

    def test_返回_prometheus_文本(self, metrics):
        status, body = metrics
        assert status == 200
        assert "# TYPE gt7_up gauge" in body
        assert re.search(r"^gt7_up 1$", body, re.M)

    def test_关键指标齐全(self, metrics):
        _, body = metrics
        for name in ("gt7_uptime_seconds", "gt7_last_packet_age_seconds",
                     "gt7_frame_cache_bytes", "gt7_sessions_total",
                     "gt7_version_info"):
            assert f"# TYPE {name}" in body, f"缺少指标 {name}"

    def test_版本号在_label_里(self, metrics, dash):
        _, body = metrics
        assert f'gt7_version_info{{version="{dash.APP_VERSION}"}} 1' in body

    def test_没有_connected_这个指标(self, metrics):
        """PS5 关机是正常状态，不是故障 —— 别做成告警指标。"""
        _, body = metrics
        assert "gt7_connected" not in body, (
            "把「游戏在不在跑」做成 0/1 指标，会让告警在正常关机时天天响")


class TestSubprocessEncoding:
    """`gen_changelog.py` 的 git 调用必须显式 UTF-8。

    🔴 真事故：Windows 上 `text=True` 用系统代码页（简中 = cp936），
       而 git 输出是 UTF-8。中文提交信息里只要出现 cp936 没有的字节，
       整个生成就抛 UnicodeDecodeError —— CI 的 windows-exe job 在
       「生成发行说明」那一步炸了，而 Linux 上永远测不出来。
    """

    def test_所有_subprocess_都指定_encoding(self):
        text = _read("tools/gen_changelog.py")
        # 找出每个 subprocess.run(...) 调用块，逐块检查
        blocks = text.split("subprocess.run(")[1:]
        assert blocks, "没找到 subprocess.run —— 脚本结构变了？"
        bad = []
        for blk in blocks:
            head = blk[:400]              # 参数都在开头
            # 只要求「会读输出」的调用：不 capture_output 的（如
            # `merge-base --is-ancestor` 只看返回码）没有解码问题。
            if "capture_output=True" not in head and "text=True" not in head:
                continue
            if "encoding=" not in head:
                bad.append(head.splitlines()[0][:60])
        assert not bad, (
            f"这些 subprocess.run 没指定 encoding：{bad}\n"
            "Windows 上会用 cp936 解码 UTF-8 输出，中文提交信息直接崩")
