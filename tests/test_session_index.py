# -*- coding: utf-8 -*-
"""场次索引（data/session_index.json）的契约测试。

背景（真实现象）：用户点「历史场次」页面一直转圈。根因是列表页要为每个
场次显示最快圈，而最快圈可能在任意一帧 —— `_best_lap_of` 只能把整个
jsonl 解析一遍。服务器上 72 个场次 / 520MB，冷启动实测 **25 秒**。

`_BEST_LAP_CACHE` 是进程内的，容器一重启就全丢，于是每次部署后第一次
点列表页都要白屏 25 秒。这里把「跨重启复用」这个行为钉死。
"""
import json
import os
import time

import pytest


def _write_session(path, laps=((1, 24000), (2, 23500)), n=40, dt=0.02):
    """写一个最小场次文件。laps = [(圈号, 该圈首帧带的 last_lap_ms)]。"""
    lines = [json.dumps({"schema": 1, "layout": "A"})]
    t = 1000.0
    for lap, ms in laps:
        for i in range(n):
            fr = {"t": t, "lap": lap, "speed_kph": 120.0,
                  "rpm": 6000.0, "throttle": 0.5, "brake": 0.0, "gear": 4,
                  "car_code": 3459}
            if i == 0 and ms:
                fr["last_lap_ms"] = ms
            lines.append(json.dumps(fr))
            t += dt
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestIndexHit:
    def test_第二次不再解析文件(self, dash, tmp_path, monkeypatch):
        """命中索引时必须直接返回，不能再去读文件内容。"""
        f = _write_session(tmp_path / "20260101_120000_a.jsonl")
        first = dash._best_lap_of(f)
        assert first == 23.5

        # 第二次：把 open_session 换成炸弹 —— 真去解析就会炸
        def boom(*a, **kw):
            raise AssertionError("命中索引后不该再打开文件")
        monkeypatch.setattr(dash, "open_session", boom)
        assert dash._best_lap_of(f) == 23.5

    def test_索引文件真的写出来了(self, dash, tmp_path):
        f = _write_session(tmp_path / "20260101_120000_a.jsonl")
        dash._best_lap_of(f)
        idx = tmp_path / "session_index.json"
        assert idx.is_file(), "算完没有落盘 —— 重启后又要重算 25 秒"
        data = json.loads(idx.read_text(encoding="utf-8"))
        row = data["sessions"]["20260101_120000_a.jsonl"]
        assert row["best"] == 23.5
        st = f.stat()
        assert row["s"] == st.st_size
        assert abs(row["m"] - st.st_mtime) < 0.01

    def test_跨进程复用_清掉内存缓存仍能命中(self, dash, tmp_path, monkeypatch):
        """模拟容器重启：内存缓存清空，只剩磁盘上的索引。"""
        f = _write_session(tmp_path / "20260101_120000_a.jsonl")
        dash._best_lap_of(f)

        with dash._CACHE_LOCK:
            dash._BEST_LAP_CACHE.clear()
        with dash._INDEX_LOCK:
            dash._INDEX_MEM.clear()

        def boom(*a, **kw):
            raise AssertionError("有磁盘索引却仍去解析文件")
        monkeypatch.setattr(dash, "open_session", boom)
        assert dash._best_lap_of(f) == 23.5


class TestIndexInvalidation:
    def test_文件变长后失效(self, dash, tmp_path):
        """活场文件还在增长 —— size 变了就必须重算。"""
        # 第 1 圈不带 last_lap_ms（那一圈是起步圈，没有"上一圈"），
        # 第 2 圈首帧带 24000 → 官方圈速 24.0s
        f = _write_session(tmp_path / "20260101_120000_a.jsonl",
                           laps=((1, None), (2, 24000)))
        assert dash._best_lap_of(f) == 24.0
        _write_session(f, laps=((1, 24000), (2, 22000)))
        assert dash._best_lap_of(f) == 22.0, "文件变了却还在用旧索引"

    def test_mtime_变但大小不变也失效(self, dash, tmp_path):
        """等长覆盖：只有 mtime 能发现。"""
        f = _write_session(tmp_path / "20260101_120000_a.jsonl")
        before = dash._best_lap_of(f)
        assert before == 23.5
        # 改写同样长度但不同的内容（把 23500 换成 21500，长度不变）
        text = f.read_text(encoding="utf-8")
        f.write_text(text.replace("23500", "21500"), encoding="utf-8")
        os.utime(f, (time.time() + 5, time.time() + 5))
        assert dash._best_lap_of(f) == 21.5, "mtime 变了却还在用旧索引"

    def test_索引损坏时回退到重算(self, dash, tmp_path):
        f = _write_session(tmp_path / "20260101_120000_a.jsonl")
        (tmp_path / "session_index.json").write_text("{不是 JSON", encoding="utf-8")
        with dash._INDEX_LOCK:
            dash._INDEX_MEM.clear()
        assert dash._best_lap_of(f) == 23.5, "索引坏了不该让整个列表页挂掉"

    def test_索引条目被删后重算(self, dash, tmp_path):
        f = _write_session(tmp_path / "20260101_120000_a.jsonl")
        dash._best_lap_of(f)
        (tmp_path / "session_index.json").unlink()
        with dash._INDEX_LOCK:
            dash._INDEX_MEM.clear()
        with dash._CACHE_LOCK:
            dash._BEST_LAP_CACHE.clear()
        assert dash._best_lap_of(f) == 23.5


class TestIndexNotASession:
    def test_索引文件不会被当成场次(self, dash, tmp_path):
        """glob_sessions 只认 *.jsonl / *.jsonl.gz，索引名不能撞上。"""
        _write_session(tmp_path / "20260101_120000_a.jsonl")
        dash._best_lap_of(tmp_path / "20260101_120000_a.jsonl")
        names = [p.name for p in dash.glob_sessions(tmp_path)]
        assert "session_index.json" not in names
        assert names == ["20260101_120000_a.jsonl"]


class TestWarm:
    def test_预热把索引补齐(self, dash, tmp_path, monkeypatch):
        """预热跑完后，列表页的每个场次都应是索引命中（不再解析）。"""
        for i in range(3):
            _write_session(tmp_path / f"2026010{i + 1}_120000_x.jsonl")
        monkeypatch.setattr(dash.time, "sleep", lambda *_: None)
        dash.warm_session_index(tmp_path)

        def boom(*a, **kw):
            raise AssertionError("预热之后仍在解析文件")
        monkeypatch.setattr(dash, "open_session", boom)
        for p in dash.glob_sessions(tmp_path):
            assert dash._best_lap_of(p) is not None

    def test_空目录不炸(self, dash, tmp_path):
        dash.warm_session_index(tmp_path)      # 不该抛


class TestAtomicWrite:
    def test_索引写失败不影响取值(self, dash, tmp_path, monkeypatch):
        """索引目录只读时（容器里常见）功能必须照常，只是下次重算。"""
        f = _write_session(tmp_path / "20260101_120000_a.jsonl")

        def boom(*a, **kw):
            raise OSError("read-only file system")
        monkeypatch.setattr(dash, "_atomic_write_json", boom)
        assert dash._best_lap_of(f) == 23.5
