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
        """🔴 这张卡必须同时出现在 **CARD_TITLES 和 DEFAULT_CARDS** 里。

        只查 CARD_TITLES 是不够的 —— 这条断言原先就只查它，于是
        「标题表里有、默认布局列表里没有」这个状态被放过了：
        `applyLayout()` 只遍历 `DEFAULT_CARDS`，不在里面的卡它压根不碰，
        结果卡片靠"没人管"偶然可见，但用户在「卡片显隐」面板里
        **找不到它、也永远没法隐藏或拖动它**（浏览器实测
        `setCardShow('c-coach', false)` 是空操作、display 仍是 block）。
        """
        assert "'c-coach':'赛道工程师'" in src        # 标题表
        assert "{id:'c-coach'" in src                 # 默认布局列表
        # 顺序也要对：教练卡在 HTML 里是第一张，默认布局里也得是第一个，
        # 否则 applyLayout 一重排它就跑位了。
        assert src.index("{id:'c-coach'") < src.index("{id:'c-rpm'")

    def test_new_cards_are_spliced_not_appended(self, src):
        """🔴 旧存档「缺啥补啥」必须按默认位置**插入**，不能 push 到末尾 ——
        push 会让新卡在升级后从第 1 张跳到最后一第，用户眼里就是 bug。"""
        i = src.index("版本升级对齐")
        seg = src[i:i + 1200]
        assert "l.cards.splice(" in seg
        assert "l.cards.push(" not in seg

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
        assert "function coachSay(text, priority)" in page
        # 语音走 say.speech（数字逐位中文），屏幕仍显示 say.text ——
        # 这条断言原先写的是 `coachSay(say.text, ...)`，加 speech 之后没跟着改，
        # 于是长期红着（红着的守卫等于没有守卫）。
        assert "const spoken = say.speech || say.text;" in page
        assert "coachSay(spoken, say.priority)" in page

    def test_no_priority_for_local_feedback(self, page):
        """本地反馈（"语音已开启"）不该触发抢占 —— 它没有优先级，
        拿 undefined 去比 `<= 0` 会是 false，正好。"""
        assert "coachSay('语音已开启')" in page


class TestBroadcastPanelWiring:
    """播报内容面板（`#coachPanelBtn` / `#coPanel`）在实时页里的接线。

    🔴 与 `#coachMute` 是两件事：那个是**全局**「要不要出声」，这个是
       **分内容**的「哪些内容出声」，两者是「与」关系（语音关着时，
       勾选多少都不会出声）。
    面板的真值在教练服务端（`/api/v1/coach/panel`）—— 这里守的是：
    按钮/容器在、读回来画、改动 POST 回去、失败时**不撤销**用户的点击。
    """

    def test_button_and_container_present(self, page):
        assert 'id="coachPanelBtn"' in page
        assert 'id="coPanel"' in page
        assert 'id="coPanel" hidden' in page, "默认收起 —— 常驻会把卡片撑长"

    def test_css_defined(self, page):
        for sel in ("#coachPanelBtn", "#coachPanelBtn.on", "#coPanel .cp-row",
                    "#coPanel .cp-row input", "#coPanel .cp-hint"):
            assert sel in page, f"缺样式 {sel}"

    def test_talks_to_the_panel_endpoint_both_ways(self, page):
        assert "/api/v1/coach/panel" in page
        assert "method: 'POST'" in page, "只读不改 = 面板是个摆设"

    def test_toggle_by_button(self, page):
        assert "function coachPanelToggle()" in page
        assert "$('coachPanelBtn').onclick = coachPanelToggle;" in page

    def test_change_uses_event_delegation(self, page):
        """行是 innerHTML 生成的 —— 逐行挂监听会在每次刷新后全部失效。"""
        assert "$('coPanel').addEventListener('change'" in page
        assert "cb.closest('.cp-row')" in page

    def test_does_not_rewrite_innerhtml_when_rows_exist(self, page):
        """🔴 重写 innerHTML 会把用户正按着的复选框整个换掉，点击像"没反应"。"""
        blk = page[page.index("function renderCoachPanel("):][:800]
        assert "box.querySelector('.cp-row')" in blk, "已有行时不能整块重画"

    def test_post_failure_uses_two_arg_then(self, page):
        """单参数 .catch 会把渲染异常也当成"发送失败"，于是回读服务端把
        用户刚点的那个勾**悄悄撤销**，而真正的原因（渲染 bug）永远看不见
        —— 与 pollCoach 同一个坑。"""
        blk = page[page.index("function setCoachMuted("):][:1000]
        assert ".then(renderCoachPanel, function(){" in blk

    def test_mute_state_is_not_kept_locally(self, page):
        """真值只在服务端：本地再存一份就会出现"两个客户端各记各的、
        谁也说服不了谁"。"""
        blk = page[page.index("function setCoachMuted("):][:1000]
        assert "localStorage" not in blk

    def test_panel_hidden_when_coach_offline(self, page):
        blk = page[page.index("function renderCoachOff("):][:800]
        assert "$('coPanel').hidden = true" in blk

    def test_refresh_piggybacks_on_the_main_poll(self, page):
        """面板的周期刷新搭主轮询的顺风车（不另起定时器）——
        教练没起来时不该多一个空转的定时器。"""
        assert "coachPanelMaybeRefresh();" in page
        assert "COACH_PANEL_REFRESH_EVERY" in page
