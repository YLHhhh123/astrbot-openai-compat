"""静态检查：模块内引用的全局名是否都有定义。

抓到过的问题：把 ``BUILTIN_STAGE_SPECS`` 重命名成 ``BUILTIN_STAGE_SPEC_BY_KEY``
后忘改 ``get_status`` 里的引用 —— 单元测试因为要 stub astrbot 跑不到那里，
只有运行时才会炸成500。这里能在提交前拦住。

在插件目录下运行：``python _static_check.py``
"""

import ast
import builtins
import io
import os
import sys

FILES = [
    "main.py",
    "stages.py",
    "output_check.py",
    "context_cleanup.py",
    "model_resolver.py",
]


def undefined_names(path: str) -> list[str]:
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)

    defined = set(dir(builtins)) | {"__name__", "__file__", "__doc__", "self", "cls"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)

    return sorted(
        {
            n.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in defined
        }
    )


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    bad = 0
    for f in FILES:
        path = os.path.join(here, f)
        if not os.path.exists(path):
            print(f"  SKIP {f}（不存在）")
            continue
        missing = undefined_names(path)
        if missing:
            print(f"  FAIL {f}: 未定义引用 {missing}")
            bad += 1
        else:
            print(f"  OK   {f}")
    print(f"\n  -> {len(FILES) - bad}/{len(FILES)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
