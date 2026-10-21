"""从 git 历史生成 CHANGELOG.md（H1），并给发版流程提供单版本说明（G2）。

为什么用脚本而不是手写：
  手写的 CHANGELOG 一定会落后。这个仓库的提交信息本来就遵循
  `type(scope): 中文描述`，直接按 type 分组就够了 —— 不需要额外的
  "记得更新 CHANGELOG" 这种靠自觉的约定。

用法：
    python tools/gen_changelog.py                 # 重写 CHANGELOG.md
    python tools/gen_changelog.py --version v1.4.2   # 只打印该版本段落
    python tools/gen_changelog.py --check         # 检查 CHANGELOG 是否落后
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "CHANGELOG.md"

# 提交类型 → 章节标题（顺序即展示顺序）
SECTIONS: list[tuple[str, str]] = [
    ("feat", "新增"),
    ("fix", "修复"),
    ("perf", "性能"),
    ("refactor", "重构"),
    ("docs", "文档"),
    ("test", "测试"),
    ("chore", "工程"),
    ("ci", "工程"),
    ("build", "工程"),
    ("style", "样式"),
]
TYPE_RE = re.compile(r"^(\w+)(?:\(([^)]*)\))?!?:\s*(.+)$")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True).stdout


def _tags() -> list[tuple[str, str]]:
    """[(tag, commit)]，按版本号升序（v1.2.10 排在 v1.2.9 之后）。"""
    out = _git("tag", "--sort=v:refname", "--format=%(refname:short)").split()
    return [(t, _git("rev-parse", f"{t}^{{commit}}").strip()) for t in out]


def _changelog_only_shas() -> set[str]:
    """只改了 CHANGELOG.md 的提交集合。

    🔴 必须排除，否则这个检查是**自指的**：生成 CHANGELOG 后提交，
    HEAD 前移，build() 里就多出一条「更新 CHANGELOG」，于是文件永远
    比 build() 少一行 —— 测试永远红。

    🔴 一次 git 调用取全部：早先按提交逐个 `git show --name-only`，
    200 个提交 = 200 次进程 spawn，整个测试从 4s 涨到 40s。
    """
    out: set[str] = set()
    cur = None
    files: list[str] = []

    def flush():
        if cur and files == ["CHANGELOG.md"]:
            out.add(cur)

    for line in _git("log", "--all", "--format=%x02%H", "--name-only").splitlines():
        if line.startswith("\x02"):
            flush()
            cur, files = line[1:], []
        elif line.strip():
            files.append(line.strip())
    flush()
    return out


_CHANGELOG_ONLY: set[str] = set()


def _commits(rev_range: str) -> list[tuple[str, str, str]]:
    """[(sha, subject, body)]"""
    raw = _git("log", "--reverse", "--format=%H%x1f%s%x1f%b%x1e", rev_range)
    out = []
    for rec in raw.split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 2:
            continue
        sha, subj = parts[0], parts[1]
        body = parts[2] if len(parts) > 2 else ""
        if sha in _CHANGELOG_ONLY:
            continue
        out.append((sha, subj, body))
    return out


def _section_body(commits: list[tuple[str, str, str]]) -> str:
    buckets: dict[str, list[str]] = {k: [] for k, _ in SECTIONS}
    other: list[str] = []
    for _sha, subj, _body in commits:
        m = TYPE_RE.match(subj.strip())
        if not m:
            other.append(subj.strip())
            continue
        typ, scope, desc = m.group(1), m.group(2), m.group(3)
        line = f"- {desc}" + (f"（`{scope}`）" if scope else "")
        buckets.setdefault(typ, []).append(line)

    out: list[str] = []
    for typ, title in SECTIONS:
        items = buckets.get(typ) or []
        if not items:
            continue
        # chore/ci/build 合并到「工程」，避免同一标题出现三次
        if title == "工程" and any(l.startswith(f"## {title}") for l in out):
            out.extend(items)
            continue
        out.append(f"### {title}")
        out.extend(items)
    if other:
        out.append("### 其他")
        out.extend(f"- {s}" for s in other)
    return "\n".join(out)


def _is_ancestor(maybe_ancestor: str, sha: str) -> bool:
    """`maybe_ancestor` 是否是 `sha` 的祖先（含自身）。"""
    r = subprocess.run(["git", "merge-base", "--is-ancestor",
                        maybe_ancestor, sha], cwd=ROOT)
    return r.returncode == 0


def _version_at(sha: str) -> str | None:
    """读某个提交里 gt7_version.py 的版本号；文件不存在返回 None。"""
    out = subprocess.run(["git", "show", f"{sha}:gt7_version.py"], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', out.stdout, re.M)
    return m.group(1) if m else None


def build() -> str:
    """按 **gt7_version.py 的版本变更**切段，而不是按 tag。

    🔴 为什么不能用 tag 切段：CHANGELOG 是**入库的文件**，而 tag 是本地/远端
       各自拥有的东西。CI 上只有推送过的 tag，开发机上可能有更多 ——
       同一份文件在两边的"期望内容"就不一样，测试永远红
       （实测：本地有 v1.2.1~v1.3.7，CI 没有，于是 v1.3.8 那一段
        在两边包含的提交数不同）。
       版本号是**代码里的事实**，任何 clone 读到的都一样，所以拿它切段。
    """
    global _CHANGELOG_ONLY
    _CHANGELOG_ONLY = _changelog_only_shas()

    commits = _commits("HEAD")                 # 旧 → 新
    if not commits:
        return "# 更新日志\n"

    # 哪些提交改过 gt7_version.py（很少，约十几个）
    ver_shas = _git("log", "--reverse", "--format=%H", "--",
                    "gt7_version.py").split()

    # 🔴 不要用 `git merge-base --is-ancestor` 逐提交判断祖先：那是
    #    一次进程 spawn，130 个提交就是 130 次，整个生成从 1s 涨到 37s。
    #    两个列表（全部提交 / 改过版本文件的提交）都是**同一个拓扑序**，
    #    直接按位置映射即可，O(n) 且零额外进程。
    pos = {sha: i for i, (sha, _s, _b) in enumerate(commits)}
    marks = sorted((pos[s], s) for s in ver_shas if s in pos)
    ver_at: dict[int, str] = {}
    for i, sha in marks:
        v = _version_at(sha)
        if v:
            ver_at[i] = v

    groups: list[tuple[str, list]] = []        # [(version, commits)]
    cur_ver: str | None = None
    for i, item in enumerate(commits):
        if i in ver_at:
            cur_ver = ver_at[i]
        key = cur_ver or "早期（无版本文件）"
        if not groups or groups[-1][0] != key:
            groups.append((key, []))
        groups[-1][1].append(item)

    parts = [
        "# 更新日志",
        "",
        "> 本文件由 `python tools/gen_changelog.py` 从 git 历史生成，**不要手改**。",
        "> 提交信息遵循 `type(scope): 描述`，脚本按 type 分组。",
        "> 分段依据是 `gt7_version.py` 的版本变更（不是 tag —— tag 各机器不一致）。",
        "",
    ]
    # 最新版本在最前；版本号相同的连续段合并（同一版本多次发版）
    merged: list[tuple[str, list]] = []
    for ver, items in groups:
        if merged and merged[-1][0] == ver:
            merged[-1][1].extend(items)
        else:
            merged.append((ver, list(items)))
    for ver, items in reversed(merged):
        title = ver if ver == "早期（无版本文件）" else f"v{ver}"
        body = _section_body(items)
        parts += [f"## {title}", "", body or "- （无变更记录）", ""]
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str]) -> int:
    if "--version" in argv:
        want = argv[argv.index("--version") + 1].lstrip("v")
        text = build()
        # 从生成结果里截出 `## vX.Y.Z` 那一段
        m = re.search(rf"^## v{re.escape(want)}\n(.*?)(?=^## |\Z)",
                      text, re.M | re.S)
        if not m:
            print(f"CHANGELOG 里没有 v{want} 的段落", file=sys.stderr)
            return 2
        print(m.group(1).strip())
        return 0

    text = build()
    if "--check" in argv:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != text:
            print("CHANGELOG.md 已落后于 git 历史，跑一次 "
                  "`python tools/gen_changelog.py`", file=sys.stderr)
            return 1
        print("CHANGELOG.md 与 git 历史一致")
        return 0

    OUT.write_text(text, encoding="utf-8")
    print(f"已写入 {OUT.relative_to(ROOT)}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
