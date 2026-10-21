# -*- coding: utf-8 -*-
"""G1：页面内联 JS 的语法检查。

为什么不在 workflow 里 grep 源码：仪表盘的 HTML 是**拼出来的**——
`HTML_PAGE` 是模板，`build_page()` 再把 CSS/JS 片段填进去，片段里还嵌着
运行期才替换的占位符。静态扫源码必然一片红。唯一可靠的做法是
**先渲染、再检查**：起一个真实服务、GET 每个页面、把 <script> 抽出来
交给 `node --check`。

没有 node 就 skip（不红）：这个仓库本身零第三方依赖，把「必须装 node 才能
提交」变成硬门槛不划算；CI 的 ubuntu-latest 自带 node，那里一定会跑。
"""
from __future__ import annotations

import http.client
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlparse

import pytest

# 只认「没有 src 的内联脚本」；带 src 的是外部资源，本地没有文件可查
_SCRIPT_RE = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>",
    re.S | re.I)


def _has_node() -> bool:
    return shutil.which("node") is not None


def _get(base: str, path: str) -> tuple[int, str]:
    u = urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=20)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read().decode("utf-8", "replace")
    finally:
        c.close()


@pytest.fixture(scope="module")
def site(dash, tmp_path_factory):
    """最小可渲染站点：一个场次 + 一个 status.json。"""
    hist = tmp_path_factory.mktemp("inline-js")
    (hist / "_trash").mkdir(exist_ok=True)
    (hist / "settings.json").write_text(
        json.dumps({"archive_after_days": 0, "trash_retention_days": 30}),
        encoding="utf-8")

    name = "20261022_010000_TestTrack.jsonl"
    frames = []
    t = 1000.0
    for lap in (1, 2):
        for i in range(600):
            ang = 2 * math.pi * i / 600
            fr = {
                "t": t + i * 0.02, "lap": lap, "speed_kph": 120.0,
                "rpm": 6000.0, "throttle": 0.6, "brake": 0.0, "gear": 4,
                "car_x": 100.0 * math.cos(ang),
                "car_z": 100.0 * math.sin(ang),
                "car_code": 3459, "gas_level": 40.0, "gas_capacity": 60.0,
                "quali_pos": 3, "num_cars": 20,
                "tyre_temp": [80.0] * 4, "susp_height": [0.05] * 4,
                "wheel_rads": [50.0] * 4, "g_force": [0.1, 0.2, 0.0],
                "has_coords": True, "layout": "A",
            }
            if i == 0 and lap == 2:
                fr["last_lap_ms"] = 24000
            frames.append(fr)
        t += 12.0
    with open(hist / name, "w", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "x", "started_at": 1000.0}) + "\n")
        for fr in frames:
            f.write(json.dumps(fr, separators=(",", ":")) + "\n")

    now = 1792599000.0
    (hist / "status.json").write_text(json.dumps({
        "schema": 2, "seq": 3, "t": now, "last_rx_t": now,
        "frame": {"t": now, "speed_kph": 100.0, "rpm": 6000, "gear": 4,
                  "throttle": 0.5, "brake": 0.0, "lap": 1, "car_code": 3459,
                  "car_x": 1.0, "car_y": 0.0, "car_z": 2.0,
                  "velocity": [27.8, 0.0, 0.0], "g_force": [0.1, 0.2, 0.0],
                  "gas_level": 50.0, "gas_capacity": 60.0, "has_coords": True,
                  "quali_pos": 3, "num_cars": 20},
        "history": [{"q": 1, "t": now, "s": 100.0}],
        "session_start": now - 10, "frames_total": 1,
        "lap_times": [[1, 24000]], "lap_fuel": [], "recording": True,
        "powertrain": "fuel", "max_energy_recovery": 0.0,
        "session_max_speed": 120.0,
    }), encoding="utf-8")

    dash.HUB = dash.TelemetryHub(hist / "status.json")
    # 🔴 实时页的 HTML 不是常量：`PAGE` 在模块里是 `""`，由 main() 调
    #    build_page() 填进去。测试不起 main()，必须自己填 ——
    #    否则 `/` 返回 200 + 空 body，看着"没报错"，实际什么都没检查。
    dash.PAGE = dash.build_page()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    srv.daemon_threads = True
    srv.history_dir = str(hist)     # type: ignore[attr-defined]
    srv.allow_hosts = ()            # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", name
    finally:
        srv.shutdown()
        srv.server_close()


def _pages(name: str) -> list[tuple[str, str]]:
    """覆盖所有「有交互」的页面。

    加新页面时记得往这里补一行 —— 漏掉的页面等于没有 JS 门禁，
    而它恰恰最可能是新写的、最容易带语法错。
    """
    f = quote(name)
    return [
        ("/", "实时页"),
        ("/sessions", "场次列表"),
        (f"/session?file={f}", "场次详情"),
        ("/tracks", "赛道列表"),
        (f"/trash-view?file={f}", "回收站查看"),
    ]


class TestInlineJsSyntax:
    def test_页面能正常渲染(self, site):
        base, name = site
        for path, label in _pages(name):
            status, body = _get(base, path)
            assert status == 200, f"{label} {path} 返回 {status}"
            assert "<html" in body.lower(), (
                f"{label} {path} 返回的不是完整 HTML（{len(body)} 字节）")

    def test_每个内联脚本都能被_node_解析(self, site):
        if not _has_node():
            pytest.skip("没有 node，跳过 JS 语法检查（CI 上一定会跑）")
        base, name = site

        checked = 0
        problems: list[str] = []
        for path, label in _pages(name):
            status, body = _get(base, path)
            assert status == 200, f"{label} {path} 返回 {status}"
            blocks = _SCRIPT_RE.findall(body)
            assert blocks, (
                f"{label} {path} 里一个内联 <script> 都没抽到 —— "
                "正则或页面结构变了，这条门禁已经失效")
            for idx, js in enumerate(blocks):
                if not js.strip():
                    continue
                checked += 1
                with tempfile.NamedTemporaryFile(
                        "w", suffix=".js", delete=False,
                        encoding="utf-8") as f:
                    f.write(js)
                    tmp = f.name
                try:
                    r = subprocess.run(["node", "--check", tmp],
                                       capture_output=True, text=True,
                                       timeout=30)
                finally:
                    Path(tmp).unlink(missing_ok=True)
                if r.returncode != 0:
                    problems.append(
                        f"{label} {path} 第 {idx + 1} 个 <script>:\n"
                        f"{(r.stderr or '').strip()[:800]}")

        assert checked > 0, "所有页面都没有内联脚本？正则失效了"
        assert not problems, (
            f"内联 JS 语法错误（共查 {checked} 段）：\n\n" + "\n\n".join(problems))
