# -*- coding: utf-8 -*-
"""P1-5：coach 卡片的两处注入面 + `?coach=` 白名单。

为什么不能只做静态子串断言：
  这个仓库对 coach 卡片的既有测试（test_coach_card.py）大多是
  「页面里有没有某个字符串」。那种断言锁不住本轮要修的东西 ——
  比如 `'<div><i>' + h.key + '</i>' + h.text + '</div>'` 这一行，
  含 `h.text` 也含 `innerHTML`，只要断言写得松，无论修没修都会绿。

  所以这里做两件事：
    1. 生成渲染函数**真的执行一次**（用 node 跑一个极小的 DOM 桩），
       把恶意 payload 喂进去，检查**产生的 DOM 结构里没有元素**；
    2. 白名单函数也真的调用，覆盖同源/跨源/非法协议/被用户拒绝 四种。
  node 不在环境里的机器上会自动 skip（不伪装成通过）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "gt7-dashboard.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ node 环境

def _node() -> str | None:
    return shutil.which("node") or (
        r"C:\Users\15657.DC-PC\.workbuddy\binaries\node\versions\22.22.2-6\node.exe"
        if Path(r"C:\Users\15657.DC-PC\.workbuddy\binaries\node\versions\22.22.2-6\node.exe").exists()
        else None)


NODE = _node()

requires_node = pytest.mark.skipif(
    NODE is None, reason="没有 node，跳过真实执行类断言（不影响静态断言）")


def _strip_js_comments(code: str) -> str:
    """去掉 // 行注释，避免"注释里提到的反模式"被当真正文。

    够用即可：不处理块注释和字符串里的 //（这些函数体里没有）。
    宁可漏剥也不要错剥 —— 错剥会把代码删掉，反而让断言变绿。
    """
    out = []
    for line in code.split("\n"):
        s = line.strip()
        if s.startswith("//"):
            continue
        out.append(line)
    return "\n".join(out)


def _extract_function(name: str) -> str:
    """从内嵌 JS 里抠出一个顶层 `function name(...) {...}` 的完整源码。

    不追求通用解析：只按花括号配对，从 `function <name>(` 开始扫到
    配平为止。注释里的括号会影响配平 —— 这些函数体内没有花括号注释，
    真有的话这里会抛错提醒，而不是静默截断。
    """
    marker = f"function {name}("
    i = SRC.index(marker)
    j = SRC.index("{", i)
    depth = 0
    k = j
    while k < len(SRC):
        ch = SRC[k]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return SRC[i:k + 1]
        k += 1
    raise AssertionError(f"{name}: 花括号没配平")


def run_js(js: str) -> dict:
    """把一段 JS 丢给 node 跑，返回它打印的 JSON。"""
    p = Path(shutil.which("echo") or ".").parent  # 随便一个已存在目录
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as f:
        f.write(js)
        path = f.name
    r = subprocess.run([NODE, path], capture_output=True, text=True,
                       encoding="utf-8", timeout=60)
    Path(path).unlink(missing_ok=True)
    if r.returncode != 0:
        raise AssertionError("node 执行失败：\n" + (r.stderr or r.stdout)[:3000])
    return json.loads(r.stdout)


# ------------------------------------------------------------------ 静态断言

class TestNoRawHtmlConcatenationInCoach:
    """静态兜底：coach 区域不允许把服务端字符串直接拼进 HTML。"""

    def test_播报历史不用_innerhtml_拼接(self):
        # 🔴 锚点必须精确到**渲染那一段**。`$('coHist')` 在文件里出现多次
        #    （折叠开关、清空、渲染），用第一个会定位到折叠逻辑，
        #    断言看着跑了、其实检查的是隔壁代码 —— 这种"错位命中"
        #    会让断言永远绿。
        i = SRC.index("(d.spoken || []).slice(0, 8)")
        blk = _strip_js_comments(SRC[i:i + 1400])
        assert "innerHTML = (d.spoken" not in blk, (
            "播报历史曾经是 innerHTML 拼字符串，h.text 来自教练服务端")
        assert "createTextNode" in blk, "应该用 textContent/createTextNode"
        # 反面：h.text 不能直接被拼进字符串
        assert "+ h.text +" not in blk

    def test_面板行对每个属性都做了转义(self):
        fn = _strip_js_comments(_extract_function("coachPanelRowHtml"))
        # 先算出转义后的变量，再往模板里插 —— 三处属性都不能出现裸 g.id / s.id
        assert "const gid = coachEsc(g.id);" in fn
        assert "const sid = coachEsc(s.id);" in fn
        # 属性模板里引用的必须是 gid / sid
        assert '"\' + gid + \'"' in fn or "' + gid + '" in fn
        # 不能再有 `+ g.id +` / `+ s.id +` 直接插进字符串
        assert "+ g.id +" not in fn, "g.id 必须经 coachEsc"
        assert "+ s.id +" not in fn, "s.id 必须经 coachEsc"
        # 但显示回退文案可以引用 s.id（它已过 coachEsc 包在外面）
        assert "coachEsc(s.label || s.id)" in fn

    def test_转义函数覆盖_html_五个元字符(self):
        fn = _extract_function("coachEsc")
        for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#x27;"):
            assert ch in fn, f"缺 {ch}"


class TestNoSelectorInjection:
    """字符串拼选择器 = 值里带引号就能注入/抛错。"""

    def test_面板回查不用拼选择器(self):
        fn = _strip_js_comments(_extract_function("renderCoachPanel"))
        assert "[data-id=" not in fn, "不能再把 g.id 拼进选择器"
        assert "dataset.id" in fn

    def test_子开关回查不用拼选择器(self):
        fn = _strip_js_comments(_extract_function("renderCoachPanel"))
        assert "[data-sub=" not in fn
        assert "dataset.sub" in fn


class TestCoachUrlWhitelist:
    def test_存在白名单函数(self):
        assert "function coachAcceptUrl(" in SRC
        assert "function coachIsSameHost(" in SRC

    def test_query_参数走白名单(self):
        i = SRC.index("let coachUrl = (function(){")
        blk = SRC[i:i + 1800]
        assert "coachAcceptUrl(q)" in blk, "?coach= 必须先过白名单"

    def test_localstorage_里的值也走白名单(self):
        """旧版本可能往 localStorage 里存过跨源地址，读回来不能直接用。"""
        i = SRC.index("let coachUrl = (function(){")
        blk = SRC[i:i + 1800]
        assert "coachAcceptUrl(v)" in blk

    def test_手填也走白名单(self):
        fn = _extract_function("askCoachUrl")
        assert "coachAcceptUrl(v)" in fn, (
            "只给 ?coach= 加校验、放过 prompt，等于门只锁了一半")

    def test_跨源要用户确认(self):
        fn = _extract_function("coachAcceptUrl")
        assert "window.confirm(" in fn, "跨源必须二次确认，不能静默接受"
        assert "只在你确认这是自己的机器时才继续" in fn


# ------------------------------------------------------------------ 真执行断言

@requires_node
class TestRenderedDomHasNoInjectedNodes:
    """真跑 coachPanelRowHtml，检查恶意 payload 没变成 DOM 结构。"""

    def _render(self, groups: list) -> str:
        fn = _extract_function("coachEsc")
        row = _extract_function("coachPanelRowHtml")
        js = f"""
{fn}
{row}
const out = groups.map(coachPanelRowHtml).join('');
process.stdout.write(JSON.stringify({{html: out}}));
"""
        # groups 以字面量注入（JSON 是合法 JS 字面量的子集，够用）
        js = js.replace("const out = groups",
                        f"const groups = {json.dumps(groups)};\nconst out = groups")
        return run_js(js)["html"]

    def test_恶意_label_被转义(self):
            html = self._render([{
                "id": "tyre", "label": "<img src=x onerror=alert(1)>",
                "desc": "d", "recent": 3, "muted": False, "subs": [],
            }])
            assert "<img" not in html
            assert "&lt;img" in html

    def test_带引号的_id_不会撑破属性(self):
        html = self._render([{
            "id": 'x" onmouseover="alert(1)',
            "label": "L", "desc": "", "recent": 0, "muted": False, "subs": [],
        }])
        # 属性值被 &quot; 包住 → 不会真的产生 onmouseover 属性
        assert 'onmouseover="alert(1)"' not in html
        assert "&quot;" in html

    def test_子开关的_id_同样被转义(self):
        html = self._render([{
            "id": "g", "label": "L", "desc": "", "recent": 0, "muted": False,
            "subs": [{"id": '"><script>x</script>', "label": "<b>", "on": True}],
        }])
        assert "<script>" not in html
        assert "<b>" not in html


@requires_node
class TestCoHistRenderedSafely:
    """播报历史：真的跑一遍渲染，检查恶意文本没变成 DOM 结构。

    与上面静态断言的区别：静态断言只能锁"用了 createTextNode 这个词"，
    而这个测试锁**行为** —— 无论换成 textContent、createTextNode、
    还是别的安全写法都能过；一旦退回拼 innerHTML 就必红。

    ⚠️ 已知局限：`_render()` 里抄了一份渲染代码（不能让 pytest 直接跑
       页面里的 <script>，那需要真 DOM 环境）。抄本与源文件会各自漂移，
       所以配了一条 `test_抄本与源文件结构一致` —— 源那边改了写法这条会红，
       提醒同步。漂移检测只比对"关键调用序列"，不比对全部文本。
    """

    def _render(self, spoken: list) -> dict:
        # 极简 DOM 桩：只实现本段用到的 createElement / createTextNode / appendChild
        js = r"""
function mkEl(tag){
  return {tag: tag, children: [], textContent: null, appendChild: function(c){
    this.children.push(c); return c; }};
}
function mkText(s){ return {nodeType: 3, text: s}; }
const document = {
  createElement: function(t){ return mkEl(t); },
  createTextNode: function(s){ return mkText(s); }
};
const ROOT = mkEl('div');
const $ = function(id){ return id === 'coHist' ? ROOT : null; };

// —— 被测代码（与页面里那段等价，故意只抄这一小段）——
function renderHist(d){
  $('coHist').innerHTML = '';
  (d.spoken || []).slice(0, 8).forEach(function(h){
    const row = document.createElement('div');
    const tag = document.createElement('i');
    tag.textContent = String(h.key || '').split('@')[0];
    row.appendChild(tag);
    row.appendChild(document.createTextNode(String(h.text == null ? '' : h.text)));
    $('coHist').appendChild(row);
  });
}

renderHist(__SPOKEN__);

// 把树扁平化成 [{tag, text}]，方便在 Python 侧断言
function flat(n, out){
  if (n.nodeType === 3) { out.push({tag: '(text)', text: n.text}); return out; }
  if (n.textContent != null) out.push({tag: n.tag, text: n.textContent});
  (n.children || []).forEach(function(c){ flat(c, out); });
  return out;
}
process.stdout.write(JSON.stringify({nodes: flat(ROOT, [])}));
"""
        js = js.replace("__SPOKEN__", json.dumps({"spoken": spoken}))
        return run_js(js)

    def test_恶意文本只作为文本节点存在(self):
        payload = "<img src=x onerror=alert(1)>"
        out = self._render([{"key": "tyre@T3", "text": payload}])
        texts = [n["text"] for n in out["nodes"] if n["tag"] == "(text)"]
        # 原文必须**原样**作为文本出现（不是被吃掉，也不是被解析成标签）
        assert payload in texts, f"文本应原样保留，实际 {texts}"
        # 树里不能出现 img 元素
        assert not [n for n in out["nodes"] if n["tag"] == "img"]

    def test_规则名作为_i_的_textContent(self):
        out = self._render([{"key": "tyre_temp@T3", "text": "刹车晚"}])
        i_nodes = [n for n in out["nodes"] if n["tag"] == "i"]
        assert len(i_nodes) == 1
        assert i_nodes[0]["text"] == "tyre_temp", "@ 前是规则名"

    def test_文本为_None_时不渲染出_null(self):
        out = self._render([{"key": "k", "text": None}])
        texts = [n["text"] for n in out["nodes"] if n["tag"] == "(text)"]
        assert "null" not in texts

    def test_抄本与源文件结构一致(self):
        """抄本漂移检测：源文件里那段渲染的关键调用序列要与抄本一致。

        只比对"调用了哪些 DOM API"，不比对字面文本 —— 改文案不该让这条红。
        真改了写法（换 textContent 之类）时这条会红，按提示同步抄本即可。
        """
        i = SRC.index("(d.spoken || []).slice(0, 8)")
        # 到下一个顶层语句结束：用 `// 面板开着时顺路刷新` 这个锚点收尾
        j = SRC.index("// 面板开着时顺路刷新", i)
        src_block = _strip_js_comments(SRC[i:j])
        for call in ("document.createElement('div')",
                     "document.createElement('i')",
                     "document.createTextNode(",
                     "appendChild("):
            assert call in src_block, f"源文件那段渲染里找不到 {call}（抄本已漂移）"


@requires_node
class TestWhitelistBehaviour:
    """真的调用 coachAcceptUrl，验证四种输入下的返回值。"""

    def _call(self, raw: str, confirm_answer: bool,
              page_url: str = "http://192.168.43.18:8787/") -> str | None:
        fn_is = _extract_function("coachIsSameHost")
        fn_ok = _extract_function("coachAcceptUrl")
        js = f"""
const LOC = new URL({json.dumps(page_url)});
const location = {{protocol: LOC.protocol, hostname: LOC.hostname,
                   origin: LOC.origin, host: LOC.host, search: ''}};
let confirmCalls = [];
const window = {{confirm: function(msg) {{
  confirmCalls.push(msg); return {"true" if confirm_answer else "false"};
}}}};
{fn_is}
{fn_ok}
const res = coachAcceptUrl({json.dumps(raw)});
process.stdout.write(JSON.stringify({{res: res, confirms: confirmCalls}}));
"""
        out = run_js(js)
        return out

    def test_同主机不同端口_静默接受(self):
        out = self._call("http://192.168.43.18:8788", confirm_answer=False)
        assert out["res"] == "http://192.168.43.18:8788"
        assert out["confirms"] == [], "同主机不该弹确认框"

    def test_跨源_用户拒绝则不采用(self):
        out = self._call("http://evil.example:8788", confirm_answer=False)
        assert out["res"] is None
        assert len(out["confirms"]) == 1

    def test_跨源_用户同意则采用(self):
        out = self._call("http://evil.example:8788", confirm_answer=True)
        assert out["res"] == "http://evil.example:8788"

    def test_非_http_协议一律拒绝(self):
        for bad in ("javascript:alert(1)", "file:///etc/passwd",
                    "data:text/html,x", "//evil.example"):
            out = self._call(bad, confirm_answer=True)
            assert out["res"] is None, f"{bad} 不该被接受"

    def test_末尾斜杠被清掉(self):
        out = self._call("http://192.168.43.18:8788///", confirm_answer=False)
        assert out["res"] == "http://192.168.43.18:8788"

    def test_空串返回_none(self):
        out = self._call("   ", confirm_answer=True)
        assert out["res"] is None
