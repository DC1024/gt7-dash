"""守住「内嵌 API 文档」与「API文档.md」两份副本不漂移。

服务通过 GET /api/v1/docs 吐出的是 gt7-dashboard.py 里的 API_DOCS_MD 常量，
仓库根目录的 API文档.md 是给人看的同一份内容。两边是**手工同步**的，
改一处忘另一处就会出现「线上文档和仓库文档不一致」这种很难发现的问题，
所以这里把关键段落拉出来做逐字比对。
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n")


@pytest.fixture(scope="module")
def embedded() -> str:
    src = _norm((ROOT / "gt7-dashboard.py").read_text(encoding="utf-8"))
    start = src.index('API_DOCS_MD = """') + len('API_DOCS_MD = """')
    end = src.index('"""', start)
    return src[start:end]


@pytest.fixture(scope="module")
def doc_file() -> str:
    return _norm((ROOT / "API文档.md").read_text(encoding="utf-8"))


def _slice(text: str, start: str, end: str) -> str:
    i, j = text.find(start), text.find(end)
    assert i >= 0, f"内嵌/文件文档里找不到段落起点：{start}"
    assert j > i, f"段落 {start} 找不到结束标记：{end}"
    return text[i:j]


# 需要逐字一致的关键段落（本轮改动都落在这里，也是最容易漏抄的地方）
_SHARED_SECTIONS = [
    ("参数", "### 参数", "## 字段与单位约定（对外承诺，只加不改）"),
    ("分段计时与轮胎滑移", "## 分段计时与轮胎滑移", "## 单圈行车轨迹"),
    ("单圈行车轨迹", "## 单圈行车轨迹", "## 圈间对比自选两圈"),
    ("圈间对比自选两圈", "## 圈间对比自选两圈", "## 赛道自动识别"),
    ("赛道自动识别", "## 赛道自动识别", "## 驾驶事件时间线"),
    ("驾驶事件时间线", "## 驾驶事件时间线", "## 进站与名次"),
    ("进站与名次", "## 进站与名次", "## 圈剖面（给赛道工程师 / 第三方按位置索引一圈）"),
    ("圈剖面", "## 圈剖面（给赛道工程师 / 第三方按位置索引一圈）", "## 使用示例"),
]


def test_profile_row_in_both_tables(embedded, doc_file):
    """圈剖面是「赛道工程师」的取数入口，表格行与段落两边都要在。"""
    row = ("| `GET /api/v1/sessions/<文件名>/profile?lap=N&step=5&prominence=12` | "
           "一圈的**按赛道位置索引**剖面：几何折线 + 等距遥测 + 刹车入点 / 弯心 / "
           "给油点。`lap` 缺省 = 最快圈 |")
    for text in (embedded, doc_file):
        assert row in text
        assert "## 圈剖面（给赛道工程师 / 第三方按位置索引一圈）" in text
        # 几何弧长 vs 速度积分这个坑必须写在文档里，否则后来者会照 /series 的口径抄
        assert "几何弧长" in text
        assert "length_by_speed_m" in text
        assert "length_drift_pct" in text


@pytest.mark.parametrize("name,start,end", _SHARED_SECTIONS)
def test_shared_paragraph_identical(embedded, doc_file, name, start, end):
    a = _slice(embedded, start, end)
    b = _slice(doc_file, start, end)
    assert a == b, f"「{name}」两份文档不一致"


def test_raceline_row_in_both_tables(embedded, doc_file):
    row = ("| `GET /api/v1/sessions/<文件名>/raceline?lap=N` | 第 N 圈的**行车轨迹**"
           "（踏板 + G 力两套着色通道）；`lap` 缺省 = 最快圈 |")
    assert row in embedded
    assert row in doc_file


def test_new_analysis_rows_in_both_tables(embedded, doc_file):
    """两个新分析接口的表格行也必须两边都有，否则线上文档查不到这个接口。"""
    rows = [
        ("| `GET /api/v1/sessions/<文件名>/sectors?n=4` | **分段计时 + 理论最快圈**；"
         "`n` = 段数（2~10，缺省 4） |"),
        ("| `GET /api/v1/sessions/<文件名>/slip?max_points=120` | **轮胎滑移**："
         "空转 / 抱死检测；每圈曲线最多 `max_points` 点 |"),
        ("| `GET /api/v1/sessions/<文件名>/deviation?ref_lap=&cmp_lap=&step=5` | "
         "**走线偏差**：本圈相对参考圈的逐米横向偏移热力图；`ref_lap` 缺省 = 最快圈，"
         "`cmp_lap` 缺省 = 最后一圈 |"),
    ]
    for row in rows:
        assert row in embedded, f"内嵌文档缺表格行：{row}"
        assert row in doc_file, f"API文档.md 缺表格行：{row}"


def test_cmp_lap_example_in_both(embedded, doc_file):
    line = ('curl "http://localhost:8787/session?file=20261007_045628_unknown_6ac5607c.jsonl'
            '&ref_lap=3&cmp_lap=7"')
    assert line in embedded
    assert line in doc_file


def test_track_identify_rows_in_both(embedded, doc_file):
    """赛道识别三个接口的行两边都要有。"""
    for text in (embedded, doc_file):
        assert "`GET /api/v1/tracks`" in text
        assert "`GET /api/v1/sessions/<文件名>/track`" in text
        assert "`POST /api/v1/tracks/<id>/rename`" in text
        assert "命中阈值取 `0.05`" in text


def test_events_row_in_both_tables(embedded, doc_file):
    row = ("| `GET /api/v1/sessions/<文件名>/events?lap=N` | "
           "**驾驶事件时间线**：打滑 / 碰撞 / 极限刹车 / 轮胎滥用 / 大油门 / 出界；"
           "`lap` 缺省 = 全部圈 |")
    assert row in embedded, "内嵌文档缺 /events 表格行"
    assert row in doc_file, "API文档.md 缺 /events 表格行"
    for text in (embedded, doc_file):
        assert "off_track` 由走线偏差主导" in text
        assert "冷路径 ~10s" in text


def test_pitstops_row_in_both_tables(embedded, doc_file):
    row = ("| `GET /api/v1/sessions/<文件名>/pitstops` | "
           "**进站与名次**：进站检测（油量环跳）/ stint 分析 / 实时名次时间线 |")
    assert row in embedded, "内嵌文档缺 /pitstops 表格行"
    assert row in doc_file, "API文档.md 缺 /pitstops 表格行"
    for text in (embedded, doc_file):
        # 两个关键诚实声明：电车不检测加油；轮胎磨损协议里没有
        assert "电车**不检测**" in text
        assert "轮胎磨损广播协议里没有" in text
        assert "当前名次" in text


def test_wheel_unit_fixed_in_both(embedded, doc_file):
    """四轮角速度的键名/单位修正在两份文档里都要到位。

    旧键 `wheel_rev_per_s` 写「转/秒」但实际是 rad/s（自标定半径 0.339/0.344 m
    就是证据）。v1 只加不改，所以正确做法是并肩加 `wheel_rad_per_s` 并把旧键
    标废弃 —— 文档里必须把这个坑写清楚，否则后来者还会照着旧键去乘 2π。
    """
    for text in (embedded, doc_file):
        assert "`wheel_rad_per_s` | rad/s" in text
        assert "已废弃" in text
        # 「转/秒」不能再作为**单位列**出现（废弃说明里提到这三个字是应该的）
        assert "| 转/秒 |" not in text


def test_history_lap_sentinel_documented(embedded, doc_file):
    """/api/v1/live 的 history[].lap 也归一了，两份文档都要写。"""
    for text in (embedded, doc_file):
        assert "所以历史窗里不会出现「第 65535 圈」" in text


def test_ref_lap_param_mentions_cmp_lap(embedded, doc_file):
    """参数说明里要同时写清 ref_lap 与 cmp_lap 的缺省值。"""
    for text in (embedded, doc_file):
        assert "`ref_lap=N`" in text
        assert "`cmp_lap=M`" in text
        assert "默认取最快圈" in text
        assert "默认取最后一圈" in text


def test_old_card_name_gone(embedded, doc_file):
    """卡片已改名「行车轨迹」，文档里不该再出现旧名。"""
    assert "参考圈赛车线" not in embedded
    assert "参考圈赛车线" not in doc_file


def test_distance_axis_caveat_documented(embedded, doc_file):
    """时间差曲线按距离对齐这个坑，两份文档都要写。"""
    for text in (embedded, doc_file):
        assert "按距离对齐" in text
