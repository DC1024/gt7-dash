#!/usr/bin/env python3
"""结构性检查：确保每个类里用到的 self.X 都在该类中被赋值过。

为什么需要这个：
我两次把字段加到错误的类里（Recorder 的方法塞进 Decoder、
_recv_by_port 加到 Decoder 却在 Recorder 里用），
而 `ast.parse` 和 `python -m py_compile` 都**发现不了**——
语法合法，只是运行时 AttributeError。

用法：
    python tools/check_attrs.py gt7-recorder.py gt7-dashboard.py
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict


def collect_assigned(cls: ast.ClassDef) -> set[str]:
    """收集类中所有「可用属性」：
    · self.X = ...（含带类型注解的 AnnAssign）
    · 类体里的字段声明（@dataclass 的字段就是这种形式）
    """
    out: set[str] = set()
    # 类体级声明：
    #   · 带注解的（@dataclass 字段）  x: int = 0
    #   · 普通赋值（类级常量/默认值）  x = 100     ← 也属于类属性
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            out.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    out.add(t.id)
    for node in ast.walk(cls):
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name):
                    if t.value.id == "self":
                        out.add(t.attr)
                elif isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    # 局部变量，不算属性
                    pass
    return out


def collect_used(cls: ast.ClassDef, assigned: set[str]) -> dict[str, list[int]]:
    """收集类中所有 self.X 的读取（排除赋值目标的左侧）。"""
    used: dict[str, list[int]] = defaultdict(list)
    for node in ast.walk(cls):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id != "self":
                continue
            # 跳过赋值/注解的目标
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                continue
            if node.attr in assigned:
                continue
            used[node.attr].append(node.lineno)
    return used


def check(path: str) -> int:
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    problems = 0

    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    # 收集所有类里定义过的属性名（跨类共享也算，避免误报方法名等）
    all_defined: set[str] = set()
    for c in classes:
        all_defined |= collect_assigned(c)
    # 方法名也算「存在」（self.method() 是合法调用）
    for c in classes:
        for m in c.body:
            if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                all_defined.add(m.name)

    print(f"\n=== {path} ===")
    for c in classes:
        assigned = collect_assigned(c)
        used = collect_used(c, assigned)
        # 过滤掉：别类定义过的（可能是 mixin/继承）、方法名、下划线魔术
        missing = {
            k: v for k, v in used.items()
            if k not in all_defined and not k.startswith("__")
        }
        has_base = bool(c.bases)
        if has_base:
            # 继承自基类：self.path / self.wfile 等来自父类，静态无法判定，跳过
            print(f"  class {c.name:18s} 定义 {len(assigned):2d} 属性  ⊘ 有基类，跳过")
            continue
        status = "✓" if not missing else f"✗ {len(missing)} 处可疑"
        print(f"  class {c.name:18s} 定义 {len(assigned):2d} 属性  {status}")
        for k, lines in sorted(missing.items()):
            print(f"      ⚠ self.{k} 未在本类赋值，使用于行 {lines[:5]}")
            problems += 1
    return problems


if __name__ == "__main__":
    files = sys.argv[1:] or ["gt7-recorder.py"]
    total = sum(check(f) for f in files)
    print()
    if total:
        print(f"发现 {total} 处可疑属性引用 —— 运行时会 AttributeError")
        sys.exit(1)
    print("✓ 所有 self.X 引用都有对应定义")
