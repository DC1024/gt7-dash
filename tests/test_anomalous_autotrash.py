"""异常场次自动归档到回收站，且进行中的活场不被误移。"""
import json
import time


def _write_session(path, frames):
    path.write_text(
        json.dumps({"header": True}) + "\n"
        + "".join(json.dumps(fr) + "\n" for fr in frames),
        encoding="utf-8",
    )


def _anomalous_frames():
    # 全是进入赛道前的菜单态（lap<=0），没有任何完成圈 → best=None → 异常
    return [{"lap": 0, "t": float(i) / 10.0} for i in range(5)]


def _normal_frames():
    # 第 1 圈跨度 25s（≥20s）→ 有效圈，非异常
    return [
        {"lap": 1, "t": 0.0},
        {"lap": 1, "t": 10.0},
        {"lap": 1, "t": 25.0},
        {"lap": 2, "t": 26.0},
    ]


def test_anomalous_auto_trashed(dash, tmp_path):
    d = tmp_path
    _write_session(d / "20260101_120000_unknown.jsonl", _anomalous_frames())
    _write_session(d / "20260101_120001_spa.jsonl", _normal_frames())

    sess = dash.list_sessions(d)
    names = {s["file"] for s in sess}

    assert "20260101_120001_spa.jsonl" in names, "正常场次应留在列表"
    assert "20260101_120000_unknown.jsonl" not in names, "异常场次应被自动移走"
    trash = d / "_trash"
    assert (trash / "20260101_120000_unknown.jsonl").exists(), \
        "异常场次应进入回收站"
    assert all(not s["anomalous"] for s in sess), \
        "主列表应无异常场次残留"


def test_live_recording_not_trashed(dash, tmp_path):
    d = tmp_path
    _write_session(d / "20260101_120000_unknown.jsonl", _anomalous_frames())
    # 正在录制，且状态文件新鲜 → list_sessions 应保护这个活场
    (d / "status.json").write_text(
        json.dumps({"recording": True, "t": time.time()}), encoding="utf-8")

    sess = dash.list_sessions(d)
    names = {s["file"] for s in sess}

    assert "20260101_120000_unknown.jsonl" in names, "活场不应被移走"
    assert not (d / "_trash" / "20260101_120000_unknown.jsonl").exists(), \
        "活场异常场次不应进回收站"
    anom = [s for s in sess if s["file"] == "20260101_120000_unknown.jsonl"]
    assert anom and anom[0]["anomalous"] is True, \
        "活场仍应标记为异常（还没跑完一圈）"
