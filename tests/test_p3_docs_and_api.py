# -*- coding: utf-8 -*-
"""P3 里"文档与 API 一致性"的守卫（F2 / F3 / B7 / H2 / H3）。

这几项的共同点：**做错了不会报错**。
  · `/api/state?full=1` 不生效 → 消费方以为拿到了全量，其实只有增量；
  · 文档说"全部接口都带 CORS" → 第三方照着写，被浏览器拦下后以为服务坏了；
  · 两套圈长容差被"统一"掉 → 完整圈被误杀，或不可比的圈被拿去逐段比；
  · 宣传站的部署步骤缺 chown → 新人照抄，首次启动写不了文件；
  · 架构手册缺章节 → 下次排查又从头读一万两千行。

所以逐条钉死。
"""
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- F2

def _make_status(hist: Path, n_frames: int = 6):
    now = 1792599000.0
    frames = [
        {"q": i, "t": now - (n_frames - i) * 0.02, "s": 100.0, "r": 6000,
         "g": 4, "th": 0.5, "bk": 0.0, "lap": 1, "wt": [80] * 4,
         "wf": [50] * 4, "gf": [0.1, 0.2, 0.0]}
        for i in range(1, n_frames + 1)
    ]
    (hist / "status.json").write_text(json.dumps({
        "schema": 2, "seq": n_frames, "t": now, "last_rx_t": now,
        "frame": {"t": now, "speed_kph": 100.0, "rpm": 6000, "gear": 4,
                  "throttle": 0.5, "brake": 0.0, "lap": 1, "car_code": 3459,
                  "car_x": 1.0, "car_y": 0.0, "car_z": 2.0,
                  "velocity": [27.8, 0.0, 0.0], "g_force": [0.1, 0.2, 0.0],
                  "gas_level": 50.0, "gas_capacity": 60.0, "has_coords": True},
        "history": frames, "full": frames,
        "session_start": now - 10, "frames_total": n_frames,
        "lap_times": [], "lap_fuel": [], "recording": True,
        "powertrain": "fuel", "max_energy_recovery": 0.0,
        "session_max_speed": 120.0,
    }), encoding="utf-8")


@pytest.fixture()
def site(dash, tmp_path):
    (tmp_path / "_trash").mkdir(exist_ok=True)
    (tmp_path / "settings.json").write_text(
        json.dumps({"archive_after_days": 0}), encoding="utf-8")
    _make_status(tmp_path, 6)
    dash.HUB = dash.TelemetryHub(tmp_path / "status.json")

    class Handler(dash.DashboardHandler):
        pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    srv.daemon_threads = True
    srv.history_dir = str(tmp_path)   # type: ignore[attr-defined]
    srv.allow_hosts = ()              # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def _get_json(base: str, path: str):
    import http.client
    from urllib.parse import urlparse
    u = urlparse(base)
    c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read().decode("utf-8"))
    finally:
        c.close()


class TestStateFullParam:
    """/api/state?full=1 必须忽略 since。"""

    def test_since_过滤掉旧帧(self, site):
        _, d = _get_json(site, "/api/state?since=3")
        qs = [f.get("q") for f in d["history"]]
        assert qs, "since=3 之后应该有帧（q=4,5,6）"
        assert all(q > 3 for q in qs), f"since 没生效：{qs}"

    def test_full_1_忽略_since(self, site):
        _, d = _get_json(site, "/api/state?since=3&full=1")
        qs = [f.get("q") for f in d["history"]]
        assert len(qs) >= 4, f"full=1 应返回整个窗口，实际只有 {qs}"
        assert min(qs) <= 3, f"full=1 没有忽略 since：{qs}"

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
    def test_full_接受多种真值(self, site, val):
        _, d = _get_json(site, f"/api/state?since=5&full={val}")
        qs = [f.get("q") for f in d["history"]]
        assert min(qs) <= 3, f"full={val} 没生效：{qs}"

    def test_full_0_不生效(self, site):
        _, d = _get_json(site, "/api/state?since=5&full=0")
        qs = [f.get("q") for f in d["history"]]
        assert all(q > 5 for q in qs), f"full=0 不该被当成真值：{qs}"


# ---------------------------------------------------------------- F3

class TestWriteEndpointCors:
    """读接口带 CORS、写接口不带 —— 文档和代码必须一致。"""

    def test_文档不再声称全部带_CORS(self):
        text = _read("API文档.md")
        assert "全部带 CORS 头" not in text, (
            "文档又回到「所有接口都带 CORS」的错误说法了 —— "
            "实际 15 个 POST 都不带，第三方照着写会被浏览器拦下")

    def test_文档说明了读写两套规则(self):
        text = _read("API文档.md")
        assert "写接口" in text and "故意不给 CORS" in text, (
            "没有说明写接口为什么不给 CORS —— "
            "下一个人会以为这是漏了，然后「顺手补上」")
        assert "不是 bug" in text, "没有明确说这是设计如此"

    def test_读接口确实带_CORS(self, site):
        import http.client
        from urllib.parse import urlparse
        u = urlparse(site)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        try:
            c.request("GET", "/api/v1/live")
            r = c.getresponse()
            assert r.getheader("Access-Control-Allow-Origin") == "*"
        finally:
            c.close()

    def test_写接口确实不带_CORS(self, site):
        import http.client
        from urllib.parse import urlparse
        u = urlparse(site)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        try:
            # 用一个一定存在的写端点：改设置。body 故意给空，只为看头。
            c.request("POST", "/api/v1/settings/archive-after",
                      body=json.dumps({"value": 0}),
                      headers={"Content-Type": "application/json"})
            r = c.getresponse()
            r.read()
            assert r.getheader("Access-Control-Allow-Origin") is None, (
                "写接口带上了 CORS —— 任何网页都能跨站改数据")
        finally:
            c.close()


# ---------------------------------------------------------------- B7

class TestToleranceDocs:
    """两套圈长容差必须被文档化，且说明「不能统一」。"""

    def test_文档写清两套容差(self):
        text = _read("API文档.md")
        assert "完整性" in text and "可比性" in text, (
            "没有区分「完整性容差」和「可比性容差」")
        assert "90%" in text and "3%" in text, "两套容差的具体数值没写"
        assert "不要把它们统一" in text, (
            "没有明确警告「别统一」—— 这正是 B7 存在的理由")

    def test_代码里两套容差数值未被统一(self):
        import gt7analysis as ga
        # 完整性：90%~125%
        assert ga._LAP_DIST_LO == 0.90
        assert ga._LAP_DIST_HI == 1.25
        # 可比性：3%
        import inspect
        sig = inspect.signature(ga.sector_times)
        assert sig.parameters["dist_tol"].default == 0.03, (
            "dist_tol 被改成了别的值 —— 若有人「统一」成 0.10/0.25，"
            "逐段对比会拿不可比的圈去比")

    def test_文档列了其它容差(self):
        text = _read("API文档.md")
        for k in ("_LAP_GEO_GAP_M", "_DEV_RMS_TOL", "_FP_LEN_TOL"):
            assert k in text, f"容差表里缺 {k}"


# ---------------------------------------------------------------- H2

class TestLandingPage:
    """宣传站不能落后于实际功能，也不能给出跑不通的部署步骤。"""

    def test_部署步骤有_chown(self):
        text = _read("docs/index.html")
        assert "chown 10001:10001" in text, (
            "部署步骤缺 chown —— 容器以 uid 10001 运行，bind mount 会遮住"
            "镜像里的 chmod，新人照抄会首次启动就写不了文件")

    def test_事件卡写的是_9_类(self):
        text = _read("docs/index.html")
        assert "9 类" in text or "9类" in text, "事件卡还写着旧的 4 类"
        for ev in ("超车", "进站", "最快圈"):
            assert ev in text, f"宣传站没提 {ev} 事件"

    def test_提到轮胎与_BoP(self):
        text = _read("docs/index.html")
        assert "/api/v1/tyres" in text, "没提轮胎参考表"
        assert "BoP" in text, "没提 BoP 档位"

    def test_提到阈值可调(self):
        text = _read("docs/index.html")
        assert "阈值" in text, "没提事件阈值可调"

    def test_版本号与真源一致(self):
        from gt7_version import __version__
        text = _read("docs/index.html")
        assert f"v{__version__}" in text, (
            f"宣传站版本不是 v{__version__}")


# ---------------------------------------------------------------- H3

class TestOpsManual:
    """架构手册必须覆盖排查需要的几块，否则等于没写。"""

    def test_手册存在(self):
        assert (ROOT / "docs" / "架构与运维.md").is_file(), (
            "没有架构手册 —— 新人要读一万两千行才能建立全局观")

    @pytest.mark.parametrize("chapter", [
        "数据流全景", "模块边界", "场次切分", "schema 2",
        "故障排查", "部署与回滚", "观测",
    ])
    def test_章节齐全(self, chapter):
        text = _read("docs/架构与运维.md")
        assert chapter in text, f"手册缺「{chapter}」一节"

    def test_写了_no_deps_规则(self):
        text = _read("docs/架构与运维.md")
        assert "--no-deps" in text, (
            "没写「必须带 --no-deps」—— 不带会连带重建 recorder，"
            "正在跑的比赛被切成两个文件")

    def test_写了_connected_不等于故障(self):
        text = _read("docs/架构与运维.md")
        assert "connected" in text and "不代表故障" in text, (
            "没提醒 connected=false 是正常状态 —— 这是最常见的误判")

    def test_README_有链接(self):
        text = _read("README.md")
        assert "docs/架构与运维.md" in text, "README 没有指向手册，没人找得到"
