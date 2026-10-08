"""详情页切圈改成「只取 compare + 就地重画」的接线检查。

以前切参考圈是 `location.href = ...` 整页刷新：整页要把 200k 帧的统计、
圈速表、四张分析卡全部重算一遍（实测热缓存 720ms / 196 KB），而实际变的
只有「哪两圈」。现在只取 /compare（8 KB / 590ms）并重画依赖它的三块。

这条接线在 Python 字符串里，静态看不出来，所以用断言把「不许整页刷新」
钉死；真浏览器的行为验证在 _ui_check/_verify_cmpxhr.cjs（要起服务 + Chromium，
不适合进 pytest）。
"""
import pytest


@pytest.fixture(scope="module")
def compare_js(dash) -> str:
    return dash._COMPARE_TMPL


def test_不许再有整页刷新(compare_js):
    """🔴 这条是最容易被"顺手改回去"的：切个圈看着也没坏，只是又慢了。"""
    assert "location.href =" not in compare_js
    assert "location.replace" not in compare_js
    assert "submit()" not in compare_js


def test_三个重画入口都在(compare_js):
    for name in ("window.drawCmpDiff", "window.drawCmpPv", "window.cmpLapChange"):
        assert name in compare_js, f"缺重画入口：{name}"


def test_CMP_必须可变(compare_js):
    """切圈是整份数据换掉，所以那份引用不能是 const。"""
    assert "var CMP = __DATA__;" in compare_js
    assert "const CMP = __DATA__;" not in compare_js


def test_切圈后地址栏要跟着变(compare_js):
    """刷新 / 收藏 / 分享链接还能拿到当前这两圈。"""
    assert "history.replaceState" in compare_js


def test_切圈不重复传赛车线(compare_js):
    """race_line 占响应的 73%，而行车轨迹卡本来就要自己取新的一圈。"""
    assert "race_line=0" in compare_js


def test_旧数据别被覆盖丢掉(dash, compare_js):
    """新响应没带 race_line，要把已有的那份留着，防止 /raceline 失败时白屏。"""
    assert "if (!d.race_line && CMP.race_line)" in compare_js


def test_走线偏差卡能被带着换参考圈(dash):
    """两张卡必须同一个参考圈，否则用户以为偏差是按第 3 圈算的、
       曲线其实是第 5 圈的。"""
    assert "window.dvSetRef" in dash._DEVIATION_TMPL


def test_compare_路由不会被通用场次路由吞掉(dash):
    """/api/v1/sessions/<名>/compare 必须排在通用分支之前。"""
    src = open(dash.__file__, encoding="utf-8").read() \
        if getattr(dash, "__file__", None) else ""
    if not src:
        import inspect
        src = inspect.getsource(dash)
    assert 'path.endswith("/compare")' in src
