# -*- coding: utf-8 -*-
"""E1：赛道候选必须**到得了用户眼前**。

`gt7analysis.course_candidates()` 按圈长从 122 条内置赛道表粗筛，
结果一直写进了 `tracks.json` 的 `course_candidates` 字段 ——
但前端从来没有读过它。算法算了、写盘了、然后被忽略。

为什么这件事重要：圈长分不出**同长度的正/逆布局**（代码注释自己承认），
所以自动命名经常给出反向的名字。而能解决这个问题的候选列表，
用户根本看不到。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conftest import load_module

dash = load_module("gt7dash_cands_under_test", "gt7-dashboard.py")

CANDS = ["Tokyo Expressway - East Clockwise",
         "Tokyo Expressway - East Counterclockwise",
         "Tokyo Expressway - Central Clockwise"]


@pytest.fixture
def lib(tmp_path):
    (tmp_path / "_trash").mkdir()
    (tmp_path / "tracks.json").write_text(json.dumps({
        "next_id": 3,
        "tracks": [
            {"id": 1, "name": "Tokyo Expressway - East Clockwise",
             "auto_named": True, "verified": False,
             "course_candidates": CANDS,
             "ref_len_m": 7301.0, "turns_cw": True,
             "desc": [], "created": "2026-10-22"},
            # 已确认的赛道：不该再出现候选
            {"id": 2, "name": "Suzuka Circuit",
             "auto_named": False, "verified": True,
             "ref_len_m": 5807.0, "turns_cw": True,
             "desc": [], "created": "2026-10-22"},
        ],
        "sessions": {},
    }, ensure_ascii=False), encoding="utf-8")
    return tmp_path


class TestCandidatesReachFrontend:
    def test_页面渲染出候选按钮(self, lib):
        html = dash.build_tracks_page(lib)
        assert "tk-cand" in html, "候选按钮一个都没有 —— E1 又回到「算完就扔」"
        for c in CANDS[:3]:
            assert c in html, f"候选 {c} 没渲染出来"

    def test_已确认的赛道不给候选(self, lib):
        html = dash.build_tracks_page(lib)
        # 只有一条 auto_named 赛道带候选，所以「圈长相近」提示只出现一次
        assert html.count("圈长相近") == 1, (
            "已确认的赛道不该再提示候选 —— 那是在质疑用户已经确认过的名字")

    def test_点选只填名字不自动保存(self, lib):
        """名字是用户的决定：点候选只填进输入框，仍需按保存。"""
        html = dash.build_tracks_page(lib)
        assert "function trackPick" in html
        pick = html[html.index("function trackPick"):]
        pick = pick[:pick.index("function trackSave")]
        assert "inp.value" in pick, "没有把候选填进输入框"
        assert "fetch(" not in pick, (
            "trackPick 里不该直接发请求 —— 点一下候选就改名，用户没有反悔机会")

    def test_候选名做了_HTML_转义(self, tmp_path):
        (tmp_path / "_trash").mkdir()
        (tmp_path / "tracks.json").write_text(json.dumps({
            "next_id": 2,
            "tracks": [{
                "id": 1, "name": "x", "auto_named": True, "verified": False,
                "course_candidates": ['<img src=x onerror=alert(1)>'],
                "ref_len_m": 1000.0, "turns_cw": True, "desc": [],
            }],
            "sessions": {},
        }, ensure_ascii=False), encoding="utf-8")
        html = dash.build_tracks_page(tmp_path)
        assert "<img src=x onerror=alert(1)>" not in html, "候选名没转义 → XSS"
        assert "&lt;img" in html


class TestApiExposesCandidates:
    def test_tracks_api_带候选字段(self, lib):
        """API 也要给 —— 第三方/未来前端同样需要这份数据。"""
        import threading
        from http.server import ThreadingHTTPServer
        import http.client
        from urllib.parse import urlparse

        s = ThreadingHTTPServer(("127.0.0.1", 0), dash.DashboardHandler)
        s.daemon_threads = True
        s.history_dir = str(lib)      # type: ignore[attr-defined]
        s.allow_hosts = ()            # type: ignore[attr-defined]
        threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            u = urlparse(f"http://127.0.0.1:{s.server_address[1]}")
            c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
            c.request("GET", "/api/v1/tracks")
            d = json.loads(c.getresponse().read().decode("utf-8"))
            c.close()
        finally:
            s.shutdown()
            s.server_close()
        tr = {t["id"]: t for t in d["tracks"]}
        assert tr[1]["course_candidates"] == CANDS
        assert tr[2]["course_candidates"] == []
