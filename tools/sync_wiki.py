#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把仓库里的 `wiki/*.md` 同步到 GitHub Wiki 仓库。

为什么要这个脚本（而不是直接把 wiki 页面也放在 wiki 仓库里手改）
------------------------------------------------------------------
同一份内容只能有一个源头，否则迟早分叉：仓库里的 `wiki/` 是**唯一源**，
GitHub Wiki 是它的**发布副本**。两边有一处必须不同 ——

  · 仓库里：链接要带扩展名 `[协议与字段](协议与字段.md)`，否则 GitHub 的
    文件视图找不到文件（它按路径找，不是按 wiki 页面名）。
  · GitHub Wiki 里：页面名**没有**扩展名，写成 `协议与字段.md` 会 404。

所以这个脚本只做一件小事：复制 + 把 `](X.md)` 改写成 `](X)`，外部链接不动。

用法
----
    # 1) 先把 wiki 仓库 clone 到本地（第一次；之后直接 pull）
    git clone https://github.com/DC1024/gt7-dash.wiki.git ../gt7-dash.wiki

    # 2) 同步（默认目标 ../gt7-dash.wiki，可用 --dest 指定）
    python tools/sync_wiki.py
    python tools/sync_wiki.py --dest D:/AI/CodeReview/gt7-dash.wiki --dry-run

    # 3) 推上去
    cd ../gt7-dash.wiki && git push

🔴 GitHub Wiki 仓库在**网页端创建第一页之前是不存在的**，此时 push 会得到
   `Repository not found`。先到仓库页点 Wiki → Create the first page，再同步。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "wiki"

# `](名字.md)` → `](名字)`；只处理「本目录的 .md」，不碰 http/https
_LOCAL_MD_LINK = re.compile(r"\]\((?![a-zA-Z]+://)([^)\s]+?)\.md\)")


def rewrite_links(text: str) -> str:
    return _LOCAL_MD_LINK.sub(r"](\1)", text)


def main() -> int:
    ap = argparse.ArgumentParser(description="同步 wiki/ 到 GitHub Wiki 仓库")
    ap.add_argument("--dest", default=str(ROOT.parent / "gt7-dash.wiki"),
                    help="wiki 仓库的本地路径（默认 ../gt7-dash.wiki）")
    ap.add_argument("--dry-run", action="store_true", help="只看会改什么，不写盘")
    args = ap.parse_args()

    dest = Path(args.dest)
    if not SRC.is_dir():
        print(f"✗ 源目录不存在：{SRC}", file=sys.stderr)
        return 1
    if not (dest / ".git").exists():
        print(f"✗ 目标不是 git 仓库：{dest}\n"
              f"  先 clone：git clone https://github.com/DC1024/gt7-dash.wiki.git {dest}",
              file=sys.stderr)
        return 1

    pages = sorted(SRC.glob("*.md"))
    if not pages:
        print(f"✗ {SRC} 里没有 .md", file=sys.stderr)
        return 1

    print(f"源：{SRC}（{len(pages)} 页） → 目标：{dest}")
    changed = 0
    for src in pages:
        raw = src.read_text(encoding="utf-8")
        out = rewrite_links(raw)
        target = dest / src.name
        old = target.read_text(encoding="utf-8") if target.exists() else None
        if old == out:
            print(f"  = {src.name}（无变化）")
            continue
        changed += 1
        action = "改" if old is not None else "增"
        print(f"  {action} {src.name}"
              + (f"（链接改写 {len(_LOCAL_MD_LINK.findall(raw))} 处）"
                 if _LOCAL_MD_LINK.search(raw) else ""))
        if not args.dry_run:
            # newline="" 保持源文件换行；git 会按 .gitattributes 归一化
            target.write_text(out, encoding="utf-8", newline="")

    if args.dry_run:
        print(f"\n[dry-run] 会改 {changed} 页，未写盘")
        return 0

    # 仓库里删掉的页面，wiki 上也要删 —— 否则会留下永远不同步的孤儿页。
    src_names = {p.name for p in pages}
    for extra in dest.glob("*.md"):
        if extra.name not in src_names:
            print(f"  删 {extra.name}（源里已没有）")
            extra.unlink()
            changed += 1

    print(f"\n完成：{changed} 页有变化。推上去：")
    print(f"  cd {dest} && git add -A && git commit -m 'wiki: 同步' && git push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
