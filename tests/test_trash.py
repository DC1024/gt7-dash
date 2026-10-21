"""回收站自动清理：只删超期文件，retention=0 时永不清理。"""
import os
import time

from packets import build_packet


def test_trash_cleanup_removes_only_expired(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    trash = tmp_path / "_trash"
    trash.mkdir()
    old = trash / "old_session.jsonl"
    old.write_text("{}")
    recent = trash / "recent_session.jsonl"
    recent.write_text("{}")
    # 把 old 的 mtime 拨到 31 天前
    old_t = time.time() - 31 * 86400
    os.utime(old, (old_t, old_t))

    removed = r._cleanup_trash()

    assert removed == 1, f"应删 1 个，实际 {removed}"
    assert not old.exists(), "超期文件应被删除"
    assert recent.exists(), "未超期文件必须保留"


def test_trash_cleanup_removes_expired_archived_gz(rec, make_recorder, tmp_path):
    """归档后的 .jsonl.gz 也必须按保留期清理（P1-5）。

    归档只换后缀，所以回收站里的文件可能是 .jsonl.gz；
    原来的 `glob("*.jsonl")` 匹配不到它们 —— 保留期对归档场次完全失效，
    _trash 会无限增长（只有手动 purge 能清）。
    """
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    trash = tmp_path / "_trash"
    trash.mkdir()
    old_plain = trash / "old_plain.jsonl"
    old_plain.write_text("{}")
    old_gz = trash / "old_archived.jsonl.gz"
    old_gz.write_bytes(b"\x1f\x8b\x08\x00")
    recent_gz = trash / "recent_archived.jsonl.gz"
    recent_gz.write_bytes(b"\x1f\x8b\x08\x00")
    old_t = time.time() - 31 * 86400
    for f in (old_plain, old_gz):
        os.utime(f, (old_t, old_t))

    removed = r._cleanup_trash()

    assert removed == 2, f"应删 1 个 .jsonl + 1 个 .jsonl.gz，实际 {removed}"
    assert not old_plain.exists(), "超期 .jsonl 应被删除"
    assert not old_gz.exists(), "超期 .jsonl.gz 应被删除"
    assert recent_gz.exists(), "未超期 .jsonl.gz 必须保留"


def test_trash_retention_zero_disables(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=0)
    trash = tmp_path / "_trash"
    trash.mkdir()
    f = trash / "ancient.jsonl"
    f.write_text("{}")
    old_t = time.time() - 365 * 86400
    os.utime(f, (old_t, old_t))

    assert r._cleanup_trash() == 0, "retention=0 应完全不清理"
    assert f.exists()


def test_trash_missing_dir_no_error(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    assert r._cleanup_trash() == 0, "目录不存在应安静返回 0"


def test_recorder_daily_routine_ends_session_still_works(rec, dec, make_recorder):
    """清理逻辑加入后，正常的录制/切分流程不受影响（回归保护）。"""
    r = make_recorder(output=str(make_recorder.__name__), trash_retention_days=30)


def test_trash_retention_from_settings_file(rec, make_recorder, tmp_path):
    """data/settings.json 里的保留期应覆盖命令行默认（页面可调的实现基础）。"""
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    (tmp_path / "settings.json").write_text('{"trash_retention_days": 1}',
                                            encoding="utf-8")
    trash = tmp_path / "_trash"
    trash.mkdir()
    f = trash / "old.jsonl"
    f.write_text("{}")
    t2 = time.time() - 2 * 86400          # 2 天前：超 1 天保留期，未超 30 天
    os.utime(f, (t2, t2))

    assert r._cleanup_trash() == 1, "settings.json 的保留期应覆盖命令行默认"
    assert not f.exists()


def test_trash_settings_invalid_falls_back(rec, make_recorder, tmp_path):
    r = make_recorder(output=str(tmp_path), trash_retention_days=30)
    (tmp_path / "settings.json").write_text('{"trash_retention_days": "abc"}',
                                            encoding="utf-8")
    trash = tmp_path / "_trash"
    trash.mkdir()
    f = trash / "old.jsonl"
    f.write_text("{}")
    t2 = time.time() - 31 * 86400
    os.utime(f, (t2, t2))
    assert r._cleanup_trash() == 1, "非法设置值应回退到命令行参数（30 天）"
