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


class TestCloudModelWiring:
    """云措辞模型的填写入口（用户自己填模型名）在实时页里的接线。

    需求背景（2026-10-09）：用户给了百炼控制台的免费额度导出表，要求
    「不要用到付费模型避免用户被收费」，并让**模型名由用户自己填**。
    真值在教练服务端的 cloud.json（`/api/v1/coach/cloud`）——
    这里守的是：入口在、只发 model（绝不发 key）、本地不存副本、
    非免费模型的警告能显示出来、以及**用户正在打字时不被周期刷新冲掉**。
    """

    def test_input_and_button_present(self, page):
        for eid in ("coModelInput", "coModelSave", "coModelMsg"):
            assert f'id="{eid}"' in page, f"缺元素 {eid}"

    def test_drawer_has_two_sections(self, page):
        """上面管「说哪些」、下面管「用哪个模型说」—— 分节是为了不让人
        以为模型名也是个播报开关。"""
        assert ">播报内容<" in page
        assert ">云措辞模型（#H）<" in page
        assert 'id="coGroups"' in page, "分组行要能单独就地更新"

    def test_shell_is_built_once(self, page):
        """骨架只在首次建一次；之后只就地改（重写会把用户正按的复选框换掉）。"""
        assert "function coachPanelShellHtml(" in page
        blk = page[page.index("function renderCoachPanel("):][:600]
        assert "coachPanelShellHtml(" in blk
        assert "box.querySelector('.cp-row')" in blk

    def test_talks_to_the_cloud_endpoint_both_ways(self, page):
        assert "/api/v1/coach/cloud" in page
        blk = page[page.index("function saveCoachModel("):][:900]
        assert "method: 'POST'" in blk, "只读不改 = 填了没用"

    def test_save_sends_the_three_compat_boxes(self, page):
        """#H：三框（model / base_url / api_key_env）一次 POST。
        红线不变：没有明文 key 字段 —— key 框收的是**环境变量名**，
        明文 key 既不进配置文件，也不该从这个框流出去。"""
        blk = page[page.index("function saveCoachModel("):][:1100]
        assert "body: JSON.stringify(bodyData)" in blk
        assert "base_url" in blk and "api_key_env" in blk
        assert "api_key:" not in blk, "只允许 api_key_env（变量名），不允许明文 key"
        assert "sk-" not in blk

    def test_save_uses_two_arg_then(self, page):
        """单参数 .catch 会把渲染异常当成"保存失败"，报一个和真实原因无关的错。"""
        blk = page[page.index("function saveCoachModel("):][:1400]
        assert ".then(function(d){" in blk and "}, function(e){" in blk

    def test_input_is_not_overwritten_while_typing(self, page):
        """🔴 抽屉开着时每 ~3s 刷一次，无条件回填会把用户正打的字冲掉。"""
        blk = page[page.index("function renderCoachCloud("):][:900]
        assert "document.activeElement !== inp" in blk

    def test_input_is_an_override_not_a_mirror(self, page):
        """输入框是**覆盖**语义：用厂商预设时必须回空串 ——
        把预设名填进去会让它在下次保存时变成一次显式覆盖。"""
        blk = page[page.index("function renderCoachCloud("):][:900]
        assert "coachCloud.model_from_user ? (coachCloud.model || '') : ''" in blk

    def test_free_status_is_shown(self, page):
        blk = page[page.index("function renderCoachCloud("):][:1200]
        assert "免费额度内" in blk
        assert "model_warning" in blk

    def test_warning_uses_the_warn_colour(self, page):
        blk = page[page.index("function renderCoachCloud("):][:1800]
        assert "'cp-hint' + (coachCloud.model_warning ? ' warn' : '')" in blk
        assert "#coPanel .cp-hint.warn" in page

    def test_preset_does_not_prefill_the_model(self, page):
        """🔴 模型名不做预设（2026-10-10 用户要求）：applyCloudPreset 只填
        端点与 key 变量名，模型名让用户自己写 —— 预设里的 model 只作为
        留空时的服务端免费默认，不能从 UI 流出去变成显式覆盖。"""
        blk = page[page.index("function applyCloudPreset("):]
        blk = blk[:blk.index("\nfunction ") if "\nfunction " in blk else 1200]
        assert "inp.value" not in blk, "预设不得回填模型名输入框"
        assert "p.model" not in blk, "预设的 model 值不得流进 UI"
        assert "自行填写" in blk, "提示语要说清模型名要用户自己填"

    def test_enter_key_saves(self, page):
        blk = page[page.index("(function initCoachCard(){"):]
        assert "ev.target.id === 'coModelInput'" in blk
        assert "saveCoachModel()" in blk

    def test_save_is_delegated(self, page):
        """抽屉内容是 innerHTML 生成的 —— 逐项挂监听会失效。"""
        blk = page[page.index("(function initCoachCard(){"):]
        assert "$('coPanel').addEventListener('click'" in blk
        assert "ev.target.id === 'coModelSave'" in blk

    def test_no_local_copy_of_the_model(self, page):
        """真值只在服务端 —— 本地再存一份就会出现"两边各记各的"。"""
        for fn in ("function saveCoachModel(", "function renderCoachCloud("):
            blk = page[page.index(fn):][:1200]
            assert "localStorage" not in blk

    def test_refresh_updates_the_model_too(self, page):
        blk = page[page.index("function coachPanelMaybeRefresh("):][:400]
        assert "loadCoachCloud(true)" in blk

    def test_css_defined(self, page):
        for sel in ("#coPanel .cp-sec", "#coPanel .cm-row",
                    "#coPanel .cm-row input", "#coPanel .cm-row button"):
            assert sel in page, f"缺样式 {sel}"

    def test_missing_endpoint_is_explained(self, page):
        """老版教练没有 /cloud → 明说并把输入框禁用，别给一个填了没反应的框。"""
        blk = page[page.index("function loadCoachCloud("):][:900]
        assert "不支持查看云模型" in blk
        assert "inp.disabled = true" in blk


class TestRuleSubToggles:
    """#G：分组下的细分开关（出界/打滑/刹车…各自独立开关）在抽屉里的接线。"""

    def test_sub_rows_rendered(self, page):
        assert "cp-sub" in page
        assert 'data-sub="' in page
        assert "function saveCoachSub(" in page

    def test_sub_toggle_posts_rules_patch(self, page):
        """子开关真值在教练端 RuleConfig —— POST 到 /config 的 rules 节。"""
        blk = page[page.index("function saveCoachSub("):][:1300]
        assert "/api/v1/coach/config" in blk
        assert "JSON.stringify({rules: patch})" in blk
        # 乐观更新 + 失败回读（与 setCoachMuted 同一套纪律）
        assert "renderCoachPanel({groups: coachPanelGroups})" in blk
        assert "loadCoachPanel(true)" in blk

    def test_sub_checkbox_delegated(self, page):
        blk = page[page.index("(function initCoachCard(){"):]
        assert "closest('.cp-sub')" in blk

    def test_sub_rows_updated_in_place(self, page):
        """周期刷新就地改子开关，不重写骨架（否则冲掉用户正点的复选框）。"""
        blk = page[page.index("function renderCoachPanel("):][:1600]
        assert "cp-sub[data-sub=" in blk
