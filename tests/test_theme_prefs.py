"""个性化主题的一致性：CSS 里的 data-theme 与前端 THEMES 列表必须对齐。

🔴 新增主题要同时改 THEME_CSS 的 [data-theme="xxx"] 和 JS 的 THEMES 数组，
   只改一侧的表现是「面板里有选项，点了没反应」。这里把这件事钉死：
   两边清单对不上，测试直接红。
"""
import re

JS_ID_RE = re.compile(r"\{id:'(\w+)',\s*name:'")


def _theme_ids(dash):
    page = dash.build_page()
    js_ids = set(JS_ID_RE.findall(page))
    # 'xxx' 只出现在 THEME_CSS 的注释里（教人怎么加主题），不是真选择器
    css_ids = set(re.findall(r'data-theme="(\w+)"', page)) - {"xxx"}
    return js_ids, css_ids


def test_themes_registered_in_both_css_and_js(dash):
    js_ids, css_ids = _theme_ids(dash)
    assert js_ids, "前端 THEMES 列表解析不到任何主题"
    assert css_ids, "CSS 里解析不到任何 data-theme 选择器"
    # light 用 :root 默认值承载，不需要单独的 CSS 块；'auto' 两边都有
    # （JS 里是面板选项，CSS 里是 prefers-color-scheme 的分支）
    assert js_ids - {"light"} == css_ids, (
        f"CSS 与 JS 主题清单不一致：仅 JS 有 {sorted(js_ids - css_ids - {'light'})}，"
        f"仅 CSS 有 {sorted(css_ids - js_ids)}")


def test_new_presets_present(dash):
    js_ids, _ = _theme_ids(dash)
    for tid in ("oled", "sepia", "racing", "glacier", "hud", "midnight"):
        assert tid in js_ids, f"缺少新主题预设 {tid}"


def test_theme_palette_completeness(dash):
    """每套主题必须给全颜色变量，缺一个就会漏出上一层的默认色。"""
    css = dash.THEME_CSS
    blocks = re.findall(r'\[data-theme="(\w+)"\]\s*\{([^}]*)\}', css)
    got = dict(blocks)
    required = ["--bg", "--card", "--line", "--text", "--muted", "--accent",
                "--accent-rgb", "--ok", "--warn", "--bad",
                "--cv-bowl", "--cv-ring", "--cv-edge", "--cv-axis",
                "--cv-text", "--cv-shadow", "--cv-grid"]
    for tid in ("dark", "oled", "sepia", "racing", "glacier", "hud", "midnight"):
        body = got.get(tid, "")
        missing = [v for v in required if v + ":" not in body.replace(" ", "")]
        assert not missing, f"主题 {tid} 缺少变量：{missing}"


def test_page_has_prefs_panel_and_persists_key(dash):
    page = dash.build_page()
    assert 'id="prefWrap"' in page, "个性化面板不见了"
    assert "gt7_prefs_v1" in page, "偏好存储 key 不见了（首屏防闪脚本依赖它）"
    assert "/*THEME_CSS*/" not in page, "占位符没被替换，主题 CSS 根本没进页面"
