# -*- coding: utf-8 -*-
"""「赛道工程师」卡片在实时页里的接线守卫。

卡片本身是内嵌 JS，没法用 pytest 直接跑 —— 所以这里守的是**接线**：
卡片在不在、几个关键 id 有没有、取数用的 `.then` 是不是双参数、
地址解析顺序对不对。这些都是"改别的东西时顺手改坏"的高发区，
而且坏了以后表现往往是**静默**的（卡片消失 / 永远显示未启动 / 主页面异常）。
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def page(dash) -> str:
    return dash.build_page()


@pytest.fixture(scope="module")
def src() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1]
            / "gt7-dashboard.py").read_text(encoding="utf-8")


class TestCardMarkup:
    def test_card_present_and_span2(self, page):
        assert 'id="c-coach"' in page
        # span2：这张卡信息量比别的多，占整行
        i = page.index('id="c-coach"')
        assert 'class="card span2"' in page[max(0, i - 60):i]

    def test_all_element_ids_present(self, page):
        for eid in ("coachDot", "coachMute", "coRef", "coS", "coDelta",
                    "coBrake", "coSay", "coHist"):
            assert f'id="{eid}"' in page, f"缺元素 {eid}"

    def test_rows_are_labelled(self, page):
        for label in ("参考圈", "本圈位置", "对比参考圈", "下一个刹车点"):
            assert label in page, label

    def test_card_is_in_layout_system(self, src):
        """不在 CARD_TITLES 里 → 布局系统的显隐/拖拽列表里没有它，
        用户第一次打开会看到一张凭空出现的卡，而且没法隐藏。"""
        assert "'c-coach':'赛道工程师'" in src

    def test_css_defined(self, page):
        for sel in ("#c-coach h2", "#coachDot", "#coachMute", ".coach-row",
                    "#coSay", "#coHist"):
            assert sel in page, f"缺样式 {sel}"

    def test_delta_color_semantics(self, page):
        """正 = 丢时间（红），负 = 更快（绿）。与圈速表的时间差同色。"""
        assert ".coach-row b.neg { color:var(--ok); }" in page
        assert ".coach-row b.pos { color:var(--bad); }" in page


class TestFetchWiring:
    def test_two_arg_then_not_catch(self, page):
        """🔴 单参数 .then(render).catch(hide) 会把 render 里的异常也当成
        "取不到数"，于是整张卡静默变成"未启动" —— 页面正常、卡没了、
        console 也无线索（这个坑在 /slip 卡片上真踩过）。"""
        assert ".then(renderCoach, renderCoachOff)" in page
        # 剥掉注释再判：注释里出现的反模式写法（上面这段就在说它）不该算数
        tail = page[page.index("function pollCoach()"):][:900]
        code = "\n".join(ln for ln in tail.split("\n")
                         if not ln.strip().startswith("//"))
        assert ".catch(" not in code, code

    def test_backoff_on_failure(self, page):
        """没起服务时会每 200ms 打一个空端口 —— 必须有退避。"""
        assert "coachRetry = Math.min(coachRetry * 2, 15000);" in page
        assert "setTimeout(pollCoach, coachRetry)" in page

    def test_recovers_after_success(self, page):
        """成功后要把退避重置，否则一次抖动会永久变成 15 秒一次。"""
        i = page.index("function renderCoach(")
        assert "coachRetry = COACH_POLL_MS;" in page[i:i + 400]

    def test_speech_deduped(self, page):
        """say 来自电平接口，会一直返回同一条；不去重就每 200ms 念一遍。"""
        assert "coachSig" in page
        assert "if (sig !== coachSig)" in page

    def test_speech_off_by_default(self, page):
        """浏览器要求先有用户手势才允许自动播放；也避免突然出声。"""
        assert "let coachSpeakOn = false;" in page

    def test_speech_guarded(self, page):
        assert "!window.speechSynthesis" in page
        assert "speechSynthesis.speak(u)" in page


class TestCoachUrlResolution:
    def test_order_is_query_then_storage_then_host(self, page):
        """🔴 默认不能只是「本页主机 + 8788」：常见部署是仪表盘在服务器、
        赛道工程师在玩家电脑（扬声器旁边），两者不同主机。"""
        i = page.index("let coachUrl = (function(){")
        blk = page[i:i + 620]
        q = blk.index("new URLSearchParams(location.search).get('coach')")
        st = blk.index("localStorage.getItem(COACH_KEY)")
        ho = blk.index("location.hostname")
        assert q < st < ho, "解析顺序必须是 ?coach= → localStorage → 同主机 :8788"

    def test_in_place_edit_persists(self, page):
        assert "function askCoachUrl()" in page
        assert "localStorage.setItem(COACH_KEY, url)" in page
        assert "$('coSay').onclick = askCoachUrl;" in page

    def test_offline_hint_shows_address(self, page):
        """未启动时必须把**实际用的地址**显示出来，否则用户无从判断
        是自己没起服务、还是地址写错了。"""
        i = page.index("function renderCoachOff(")
        assert "赛道工程师未启动（' + coachUrl + '）" in page[i:i + 500]

    def test_cors_needed_note(self, src):
        """跨源取数依赖对方给 CORS 头 —— 这条要留注释，否则以后有人
        为了"省一次往返"把 coach 挪进同源，会发现改不动。"""
        i = src.index("赛道工程师（gt7-coach）：状态卡片")
        assert "CORS" in src[i:i + 300]


class TestVoicePreemption:
    """P0 要能**打断**正在念的闲话。

    浏览器 TTS 默认是排队制：一句 delta（P3）会把随后的"出界"（P0）
    堵在它后面，等念完就晚了。真赛车无线电是抢麦，不是排队。
    """

    def test_cancels_on_p0(self, page):
        assert "speechSynthesis.cancel()" in page
        assert "priority <= 0" in page, "只有 P0 才抢占"

    def test_priority_is_passed_through(self, page):
        """不把 priority 传进去，抢占逻辑等于没写。"""
        assert "coachSay(say.text, say.priority)" in page
        assert "function coachSay(text, priority)" in page

    def test_no_priority_for_local_feedback(self, page):
        """本地反馈（"语音已开启"）不该触发抢占 —— 它没有优先级，
        拿 undefined 去比 `<= 0` 会是 false，正好。"""
        assert "coachSay('语音已开启')" in page
