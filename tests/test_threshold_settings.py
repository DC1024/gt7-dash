# -*- coding: utf-8 -*-
"""B3：事件阈值运行时可调。

背景：`Thresholds` 一直是 dataclass，注释写「全部可调，按自己的口味改」——
实际意思是**改源码再重启**。检测判据全依赖这些数，不同车型/赛道/风格差别
很大，不能调等于把用户锁死在作者的默认值上。

这里盯死四件事：
  1. 没设置时 = 默认值；
  2. 只存**改过的**键（升级新增阈值不会被旧设置卡住）；
  3. 脏设置不能把事件卡打挂（未知键忽略、非法值忽略）；
  4. **阈值变了必须让事件缓存失效** —— 否则用户改完看到的是旧结果，
     会以为"改了没用"。
"""
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import load_module

dash = load_module("gt7dash_threshold_under_test", "gt7-dashboard.py")


@pytest.fixture
def hist(tmp_path):
    (tmp_path / "_trash").mkdir()
    (tmp_path / "settings.json").write_text("{}", encoding="utf-8")
    return tmp_path


class TestThresholdHelpers:
    def test_没设置时等于默认(self, hist):
        eff = dash.event_thresholds(hist)
        assert eff, "阈值字典不该为空（检测器要能加载）"
        assert dash._threshold_overrides(hist) == {}

    def test_只存改过的键(self, hist):
        s = json.loads((hist / "settings.json").read_text(encoding="utf-8"))
        s["event_thresholds"] = {"spin_lat_g": 1.3}
        (hist / "settings.json").write_text(json.dumps(s), encoding="utf-8")
        assert dash._threshold_overrides(hist) == {"spin_lat_g": 1.3}

    def test_未知键被忽略(self, hist):
        s = {"event_thresholds": {"spin_lat_g": 1.3, "no_such_key": 9}}
        (hist / "settings.json").write_text(json.dumps(s), encoding="utf-8")
        eff = dash.event_thresholds(hist)
        assert "no_such_key" not in eff
        assert eff["spin_lat_g"] == 1.3

    def test_非法值被忽略而不是抛异常(self, hist):
        s = {"event_thresholds": {"spin_lat_g": "abc", "impact_long_g": 1e9}}
        (hist / "settings.json").write_text(json.dumps(s), encoding="utf-8")
        eff = dash.event_thresholds(hist)      # 不抛
        assert eff["spin_lat_g"] == dash._threshold_fields()["spin_lat_g"]
        assert eff["impact_long_g"] == dash._threshold_fields()["impact_long_g"]

    def test_指纹随阈值变化(self, hist):
        a = dash._threshold_fingerprint(hist)
        s = {"event_thresholds": {"spin_lat_g": 1.3}}
        (hist / "settings.json").write_text(json.dumps(s), encoding="utf-8")
        b = dash._threshold_fingerprint(hist)
        assert a != b, "阈值变了指纹没变 → 事件卡会一直命中旧缓存"


class TestThresholdApi:
    @pytest.fixture
    def srv(self, hist):
        s = ThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
        s.daemon_threads = True
        s.history_dir = str(hist)     # type: ignore[attr-defined]
        s.allow_hosts = ()            # type: ignore[attr-defined]
        threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            yield f"http://127.0.0.1:{s.server_address[1]}"
        finally:
            s.shutdown()
            s.server_close()

    def _req(self, base, path, body=None):
        import http.client
        from urllib.parse import urlparse
        u = urlparse(base)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        try:
            if body is None:
                c.request("GET", path)
            else:
                c.request("POST", path, json.dumps(body),
                          {"Content-Type": "application/json"})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode("utf-8"))
        finally:
            c.close()

    def test_GET_返回阈值与默认值(self, srv):
        st, d = self._req(srv, "/api/v1/settings")
        assert st == 200
        assert "event_thresholds" in d
        assert "event_thresholds_default" in d
        assert d["event_thresholds"] == d["event_thresholds_default"]
        assert any(it["key"] == "spin_lat_g" for it in d["event_thresholds_ui"])

    def test_POST_改一个值(self, srv):
        st, d = self._req(srv, "/api/v1/settings/event-thresholds",
                          {"values": {"spin_lat_g": 1.42}})
        assert st == 200, d
        assert d["event_thresholds"]["spin_lat_g"] == pytest.approx(1.42)
        assert "spin_lat_g" in d["overridden"]
        # 落盘了
        _, d2 = self._req(srv, "/api/v1/settings")
        assert d2["event_thresholds"]["spin_lat_g"] == pytest.approx(1.42)

    def test_POST_null_恢复默认(self, srv):
        self._req(srv, "/api/v1/settings/event-thresholds",
                  {"values": {"spin_lat_g": 1.42}})
        st, d = self._req(srv, "/api/v1/settings/event-thresholds",
                          {"values": {"spin_lat_g": None}})
        assert st == 200
        assert d["event_thresholds"]["spin_lat_g"] == pytest.approx(
            d and dash._threshold_fields()["spin_lat_g"])
        assert d["overridden"] == []

    def test_未知阈值名返回_400(self, srv):
        st, d = self._req(srv, "/api/v1/settings/event-thresholds",
                          {"values": {"nope": 1}})
        assert st == 400
        assert "不认识的阈值名" in d["error"]

    def test_非数字返回_400(self, srv):
        st, d = self._req(srv, "/api/v1/settings/event-thresholds",
                          {"values": {"spin_lat_g": "abc"}})
        assert st == 400

    def test_body_结构不对返回_400(self, srv):
        st, d = self._req(srv, "/api/v1/settings/event-thresholds",
                          {"value": 1})
        assert st == 400
