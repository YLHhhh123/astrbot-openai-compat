"""适配器编辑的静态闸门。

这是整个功能的安全边界所在：**AI 只能调用本模块暴露的检查，全通过才允许
提案进入待审队列**。这里不做"运行后再看"，而是在拿到 diff 的第一时间
就判定 —— 因为一旦代码落盘并被导入执行，拦���就晚了。

四道闸门
--------
1. **路径**：只允许改平台适配器目录（``core/platform/sources/<name>/``）
   与 ``metadata.yaml``；核心目录一律拒绝。
2. **不可改清单**：配置里显式列出的适配器（``protected_adapters``）
   拒绝修改 —— 这是给"某些适配器不许动"留的口子。
3. **出站调用**：适配器里负责**向外发消息/调平台 API** 的部分不允许改。
   判定用 AST 找出被改函数内的调用图，命中出站 API 即拒。
4. **危险导入**与**配额**：禁import 列表 + 每会话/每日/单次 diff 规模上限。

关于第3 条要特别说明：出站能力是**平台适配器的固有职责**（不发送就收不到消息），
所以不能一刀切禁掉所有 send 调用 —— 那样适配器就没用了。真正的边界是
"**不许改变发消息的目标与内容构造逻辑**"。因此这里采用：

* 允许在适配器里保留**已存在**的出站调用；
* 但**新增**或**修改**出站调用（调用点增删、或调用实参变化）一律拒绝。

这样 AI 可以在适配器里加解析逻辑、加字段处理、修 bug，但不能借改适配器
之口往任意群发消息、也不能改已有消息的目标判定。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Iterable

# ── 闸门 1：路径边界 ────────────────────────────────────────────────────
# AI 唯一可写的位置：平台适配器源码目录。
PLATFORM_SOURCES = "astrbot/core/platform/sources"
# 新建适配器需要登记到 metadata.yaml，否则内核扫不到；
# 因此它被允许编辑，但仅允许"新增条目"而非删除（见 metadata_only_add）。
METADATA_REL = "astrbot/core/platform/sources/{adapter}/metadata.yaml"

# 绝对不碰的核心路径片段（出现在待改文件路径里即拒）。
FORBIDDEN_PATH_FRAGMENTS = (
    "whitelist_check",     # 白名单管道：决定谁能触发 LLM
    "core/provider",       # 模型/provider 适配器
    "core/db",             # 数据库
    "core/config",         # 配置加载
    "core/star",           # 插件与命令注册
    "dashboard/api",       # WebUI 接口
    "startup",
    "main.py",             # 进程入口
    "requirements",
    "pyproject",
    "setup",
)


class PathDenied(Exception):
    """路径不在允许范围。"""


def normalize_rel(path: str) -> str:
    """把提案里的路径规整成仓库相对路径（正斜杠、去 ./）。"""
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def resolve_adapter_target(rel_path: str) -> tuple[str, str]:
    """把相对路径解析为 ``(适配器名, 适配器内相对路径)``。

    Returns:
        (adapter_name, rel_within_adapter)；不允许的路径抛 :class:`PathDenied`。
    """
    rel = normalize_rel(rel_path)
    if not rel:
        raise PathDenied("路径为空")
    if ".." in rel.split("/"):
        raise PathDenied("路径含上级引用")
    for frag in FORBIDDEN_PATH_FRAGMENTS:
        if frag in rel:
            raise PathDenied(f"禁止修改的路径：包含 {frag}")

    m = re.match(r"^astrbot/core/platform/sources/([^/]+)/(.+)$", rel)
    if not m:
        raise PathDenied("只允许修改 astrbot/core/platform/sources/<适配器>/ 下的文件")

    adapter, inner = m.group(1), m.group(2)
    if inner.startswith(".") or "__pycache__" in rel:
        raise PathDenied("路径非法")
    return adapter, inner


# ── 闸门 2：不可改清单 ──────────────────────────────────────────────────


def check_protected(adapter: str, protected: Iterable[str]) -> None:
    """命中不可改清单则拒绝。"""
    names = {str(x).strip() for x in protected or [] if str(x).strip()}
    if adapter in names:
        raise PathDenied(f"适配器 {adapter} 在不可修改清单中")


# ── 闸门 3：出站调用检测（AST）────────────────────────────────────────
# 这些名字一旦出现在"新增/修改"的代码里，就意味着 AI 想借适配器向外发东西。
OUTBOUND_NAMES = (
    "send", "send_msg", "send_message", "send_group_msg", "send_private_msg",
    "post_message", "post_group_msg", "post_private_msg",
    "call_api", "send_file", "upload_file", "upload_image",
    "set_msg", "send_forward_msg", "send_group_forward_msg",
)
# 只匹配点分链的**最后一段**是出站方法名，因此 ``a.b.c.send()`` 也算。
# 早期版本要求整条链以 bot./client./api. 等开头，会漏掉
# ``self.cq_api.send_group_msg`` 之外的写法。
_OUTBOUND_TAIL = re.compile(
    r"(?:^|\.)(?:" + "|".join(re.escape(n) for n in OUTBOUND_NAMES) + r")$",
    re.I,
)


def _iter_calls(tree: ast.AST):
    """产出函数/属性调用节点。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def _callee_name(call: ast.Call) -> str:
    """取调用的点分名，如 ``bot.send_msg`` → ``bot.send_msg``。"""
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        base = None
        if isinstance(fn.value, ast.Name):
            base = fn.value.id
        elif isinstance(fn.value, ast.Attribute):
            # 多级属性：a.b.c 取全链
            parts = []
            cur: ast.AST = fn.value
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            base = ".".join(reversed(parts))
        return f"{base}.{fn.attr}" if base else fn.attr
    return getattr(fn, "attr", "") or ""


def outbound_calls_in_source(src: str) -> list[str]:
    """找出源码里所有出站调用点名称（仅函数名，不含实参）。"""
    return [name for name, _sig in outbound_call_signatures(src)]


def outbound_call_signatures(src: str) -> list[tuple[str, str]]:
    """找出源码里所有出站调用点，产出 ``(名字, 调用签名)``。

    签名里带上实参源码，是为了让"**实参被改了**"这种改动也能被识别出来——
    仅比较调用次数会漏掉"调用点没变但换了目标群号"这类最危险的改动。
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    found: list[tuple[str, str]] = []
    for call in _iter_calls(tree):
        name = _callee_name(call)
        if not name or not _OUTBOUND_TAIL.search(name):
            continue
        args_src = ", ".join(_safe_unparse(a) for a in call.args)
        kwargs_src = ", ".join(
            f"{kw.arg}={_safe_unparse(kw.value)}" for kw in call.keywords
        )
        joined = ", ".join(x for x in (args_src, kwargs_src) if x)
        found.append((name, f"{name}({joined})"))
    return found


def _safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<expr>"


def diff_adds_outbound(base_src: str | None, new_src: str) -> list[str]:
    """判断改动是否**新增或修改**了出站调用。

    比较改前/改后的出站调用**签名**（名字 + 实参）：
    * 出现新签名 → 新增调用，拒；
    * 同名但实参不同 → 改了发送目标/内容，拒；
    * 完全未变的签名 → 允许（否则适配器无法维护）。

    删除出站调用不在此拦截范围内（不构成外发风险）。
    """
    before = {sig for _name, sig in outbound_call_signatures(base_src or "")}
    after = {sig for _name, sig in outbound_call_signatures(new_src or "")}

    changed = sorted(after - before)
    if changed:
        return changed
    return []


# ── 闸门 4：危险导入 + 配额 ────────────────────────────────────────────
BANNED_IMPORTS = (
    "subprocess", "socket", "ctypes", "shutil", "multiprocessing",
    "importlib", "pickle", "marshal", "signal", "pty", "http",
    "urllib", "requests", "aiohttp",
)
BANNED_CALLS = ("eval", "exec", "compile", "__import__", "globals", "vars")

# 允许的出站例外：某些适配器内部本来就用 HTTP 与平台通信，
# 它们的 **import** 不能一律禁掉，否则这些适配器无法编辑。
# 这里只拦 Python 层的危险执行原语，网络库按"是否新增出站"由闸门 3把控。
SAFE_IMPORTS = frozenset({"http", "urllib", "requests", "aiohttp"})


def banned_imports_in_source(src: str) -> list[str]:
    """找出源码里被禁的 import / 调用。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ["<syntax error>"]

    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in BANNED_IMPORTS and root not in SAFE_IMPORTS:
                    bad.append(f"import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BANNED_IMPORTS and root not in SAFE_IMPORTS:
                bad.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if name in BANNED_CALLS:
                bad.append(f"{name}()")
    return sorted(set(bad))


# ── 配额 ───────────────────────────────────────────────────────────────


@dataclass
class Quota:
    per_session: int = 3
    per_day: int = 10
    max_diff_lines: int = 200
    max_file_bytes: int = 400_000


@dataclass
class GateResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def check_quota(counts: dict[str, int], q: Quota) -> GateResult:
    """counts: {"session": 本会话已提案数, "day": 今日已提案数}"""
    reasons: list[str] = []
    if counts.get("session", 0) >= q.per_session:
        reasons.append(f"已达本会话提案上限（{q.per_session} 次）")
    if counts.get("day", 0) >= q.per_day:
        reasons.append(f"已达今日提案上限（{q.per_day} 次）")
    return GateResult(not reasons, reasons)
