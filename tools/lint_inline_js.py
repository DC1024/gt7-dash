"""把页面里渲染后的内联 <script> 抽成 .js 文件，供 ESLint 检查。

为什么要渲染后再抽：仪表盘的 HTML 是模板 + 占位符拼出来的，源码里
`{json.dumps(x)}` 这种片段不是合法 JS。静态扫源码必然一片红，只能先
起服务拿真实 HTML。

用法：
    python tools/lint_inline_js.py <输出目录>
"""
from __future__ import annotations

import json
import re
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

_SCRIPT_RE = re.compile(
    r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script>", re.S | re.I)


def _load_dash():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gt7dash_lint", ROOT / "gt7-dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gt7dash_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


def main(outdir: Path) -> int:
    import tempfile
    dash = _load_dash()

    hist = Path(tempfile.mkdtemp(prefix="lint-js-"))
    (hist / "_trash").mkdir()
    (hist / "settings.json").write_text("{}", encoding="utf-8")
    name = "20261022_010000_LintTrack.jsonl"
    with open(hist / name, "w", encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "x", "started_at": 1000.0}) + "\n")
        # 两圈：单圈场次在详情页会走「没有有效圈」的降级分支，
        # 那段 JS 反而测不到。
        import math
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
                f.write(json.dumps(fr, separators=(",", ":")) + "\n")
            t += 12.0

    dash.HUB = dash.TelemetryHub(hist / "status.json")
    dash.PAGE = dash.build_page()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
    srv.daemon_threads = True
    srv.history_dir = str(hist)     # type: ignore[attr-defined]
    srv.allow_hosts = ()            # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    outdir.mkdir(parents=True, exist_ok=True)
    pages = [
        ("/", "live"),
        ("/sessions", "sessions"),
        (f"/session?file={name}", "session"),
        ("/tracks", "tracks"),
        (f"/trash-view?file={name}", "trash"),
    ]
    n = 0
    try:
        import http.client
        from urllib.parse import quote
        port = srv.server_address[1]
        for path, label in pages:
            path = path.replace(name, quote(name))
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
            c.request("GET", path)
            body = c.getresponse().read().decode("utf-8", "replace")
            c.close()
            blocks = [b for b in _SCRIPT_RE.findall(body) if b.strip()]
            if not blocks:
                continue
            # 拼成一个文件：页面内的 <script> 共享作用域，分开检查会
            # 把跨块调用误报成 no-undef。
            (outdir / f"{label}.js").write_text(
                "\n;\n".join(blocks), encoding="utf-8")
            n += 1
    finally:
        srv.shutdown()
        srv.server_close()
    print(f"已抽取 {n} 个页面的内联脚本 → {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "_lint_js")))
