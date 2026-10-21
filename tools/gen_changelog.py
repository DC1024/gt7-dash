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


def build() -> str:
    global _CHANGELOG_ONLY
    _CHANGELOG_ONLY = _changelog_only_shas()
    tags = _tags()
    parts = [
        "# 更新日志",
        "",
        "> 本文件由 `python tools/gen_changelog.py` 从 git 历史生成，**不要手改**。",
        "> 提交信息遵循 `type(scope): 描述`，脚本按 type 分组。",
        "",
    ]
    # 最新 tag 之后（未发版）的提交放最前
    if tags:
        head = _git("rev-parse", "HEAD").strip()
        if head != tags[-1][1]:
            body = _section_body(_commits(f"{tags[-1][0]}..HEAD"))
            if body:
                parts += ["## 未发布", "", body, ""]
        # 🔴 范围要按 tag 在**升序列表里的前一个**算，不能靠循环变量累加：
        #    从最新往回遍历时，第一次循环还没有 prev，会把整条历史都算进
        #    最新那一版（第一次生成时就踩了，v1.4.1 段里塞了 100+ 条）。
        for i in range(len(tags) - 1, -1, -1):
            name, sha = tags[i]
            rng = f"{tags[i - 1][0]}..{sha}" if i else sha
            body = _section_body(_commits(rng))
            parts += [f"## {name}", "", body or "- （无变更记录）", ""]
    else:
        parts += [_section_body(_commits("HEAD"))]
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str]) -> int:
    if "--version" in argv:
        tag = argv[argv.index("--version") + 1]
        tags = _tags()
        idx = [i for i, (n, _) in enumerate(tags) if n == tag]
        if not idx:
            print(f"没有 tag {tag}", file=sys.stderr)
            return 2
        i = idx[0]
        prev = tags[i - 1][0] if i else None
        rng = f"{prev}..{tags[i][0]}" if prev else tags[i][0]
        print(_section_body(_commits(rng)))
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
