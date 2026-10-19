"""模型分组：把「组名」当成 provider id 用，插件零改动即可接入。

设计要点
--------
**为什么放在内核**：插件在配置里写 ``HomeAPI/ministral-14b-latest`` 这种裸id，
换模型要逐个插件改。分组让插件写 ``daily`` 即可，而**解析发生在内核
``ProviderManager._resolve_using_provider``** 里 —— 所有取模型的路径
（同步 / 异步 / 会话隔离 / 全局默认）都汇聚到那一个函数，所以插件不用改。

**递归**：组可以引用组（``长文: [编程, gemini-long]``），展开时带循环检测，
``a -> b -> a`` 会报错并跳过该分支，而不是无限递归。

**两种轮换策略**（每组独立设置）：

* ``fallback``（默认）—— 取组内第一个可用；主模型失败时内核已有的
  ``fallback_provider_ids`` 会接手，本组只负责"平时用谁"。
* ``round_robin`` —— 每次请求往后轮一位，用于分摊负载。轮询位置存
  sp（``model_group_rr.<组名>``），重启后归零。

**语义参数**（范围③）：分组可带 ``temperature`` / ``max_output_tokens`` /
``context_window`` 等**语义参数**，由 :func:`map_params_to_provider` 按厂商
翻译成实际字段名。因为 ``max_tokens`` 在 Gemini 里叫 ``max_output_tokens``，
原样透传会静默失效。
"""

from __future__ import annotations

from typing import Any

from astrbot.core import logger

STRATEGY_FALLBACK = "fallback"
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGIES = (STRATEGY_FALLBACK, STRATEGY_ROUND_ROBIN)

RR_KEY_PREFIX = "model_group_rr."

# ── 语义参数 → 各厂商字段名 ────────────────────────────────────────────
# 键是语义名，值是 {厂商: 字段名}。缺厂商键时用 "*" 作为通用兜底。
PARAM_VENDOR_MAP: dict[str, dict[str, str]] = {
    "temperature": {"*": "temperature"},
    "max_output_tokens": {
        "*": "max_tokens",
        "gemini": "max_output_tokens",
    },
    "top_p": {"*": "top_p"},
    "presence_penalty": {"*": "presence_penalty"},
    "frequency_penalty": {"*": "frequency_penalty"},
    "seed": {"*": "seed"},
    "stop_sequences": {"*": "stop"},
}

# 厂商识别关键字（provider_source_id / provider id 里出现即算命中）
VENDOR_HINTS: tuple[tuple[str, str], ...] = (
    ("gemini", "gemini"),
    ("google", "gemini"),
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("qwen", "qwen"),
    ("dashscope", "qwen"),
    ("glm", "zhipu"),
    ("zhipu", "zhipu"),
    ("deepseek", "deepseek"),
    ("moonshot", "moonshot"),
    ("kimi", "moonshot"),
)


def detect_vendor(provider_id: str, source_id: str = "") -> str:
    """从 provider id / source id 猜厂商，用于参数映射。识别不出返回空串。"""
    hay = f"{provider_id} {source_id}".lower()
    for hint, vendor in VENDOR_HINTS:
        if hint in hay:
            return vendor
    return ""


def map_params_to_provider(
    params: dict[str, Any], provider_id: str = "", source_id: str = ""
) -> dict[str, Any]:
    """把语义参数翻译成目标厂商认识的字段名。

    未在映射表里的参数按原名透传（用户可能要用厂商私有参数）。

    翻译后若与既有请求字段冲突，**以分组参数为准**——这正是"策略"的意思。
    """
    if not isinstance(params, dict) or not params:
        return {}
    vendor = detect_vendor(provider_id, source_id)
    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        m = PARAM_VENDOR_MAP.get(key)
        if not m:
            out[key] = value
            continue
        field = m.get(vendor) or m.get("*")
        if field:
            out[field] = value
    return out


# ── 分组数据（来自 cmd_config 的 model_groups）───────────────────────────


class GroupError(Exception):
    """分组配置有误（引用了不存在的组、循环引用等）。"""


def _normalize_group(raw: dict) -> dict:
    """规整一个分组定义，容忍各种字段缺失。"""
    name = str(raw.get("name") or "").strip()
    members = raw.get("members")
    if not isinstance(members, list):
        members = []
    members = [str(m).strip() for m in members if str(m).strip()]

    strategy = str(raw.get("strategy") or STRATEGY_FALLBACK).strip().lower()
    if strategy not in STRATEGIES:
        strategy = STRATEGY_FALLBACK

    params = raw.get("params")
    if not isinstance(params, dict):
        params = {}

    scope = raw.get("scope")
    scope = scope if isinstance(scope, dict) else {}

    return {
        "name": name,
        "label": str(raw.get("label") or name),
        "members": members,
        "strategy": strategy,
        "params": params,
        "scope": scope,
        "enabled": bool(raw.get("enabled", True)),
        "note": str(raw.get("note") or ""),
    }


def load_groups(config: dict) -> dict[str, dict]:
    """从配置里读出全部分组，返回 ``{组名: 定义}``。"""
    raw = config.get("model_groups")
    out: dict[str, dict] = {}
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        g = _normalize_group(item)
        if g["name"]:
            out[g["name"]] = g
    return out


def expand_group(name: str, groups: dict[str, dict], _seen: tuple[str, ...] = ()) -> list[str]:
    """把组名展开成有序的 provider id 列表（递归解析嵌套）。

    Args:
        name: 组名，或直接是provider id。
        groups: 全部组定义。
        _seen: 递归路径上的组名，用于检测循环。

    Returns:
        去重后的 provider id 列表，保持声明顺序。
        组名不存在时**按provider id 处理**——这样配置里写裸 id 也仍然有效，
        迁移期不会出现"必须先把 id 建成组"的硬要求。

    Raises:
        GroupError: 循环引用。
    """
    key = str(name or "").strip()
    if not key:
        return []

    g = groups.get(key)
    if g is None:
        # 不是组名 → 当作裸 provider id（兼容既有配置）
        return [key]

    if key in _seen:
        chain = " -> ".join([*_seen, key])
        raise GroupError(f"检测到循环引用：{chain}")

    if not g["enabled"]:
        logger.debug(f"[model_group] 组 {key} 已禁用，展开为空")
        return []

    out: list[str] = []
    for member in g["members"]:
        for pid in expand_group(member, groups, (*_seen, key)):
            if pid not in out:
                out.append(pid)
    return out


def find_group_for_scope(
    groups: dict[str, dict], umo: str | None, group_id: str | None = None
) -> str | None:
    """按适用范围找出应使用的组名。

    优先级：会话显式绑定 > 群绑定 > 全局默认。
    """
    if group_id:
        return group_id
    if not isinstance(groups, dict) or not groups:
        return None

    # 无 umo（如后台任务、WebUI 测试）时也应能用全局默认组
    gid = _group_of_umo(umo) if umo else ""
    if gid:
        for name, g in groups.items():
            if not g["enabled"]:
                continue
            scope = g["scope"] or {}
            umo_scope = scope.get("umo")
            if isinstance(umo_scope, list) and gid in [str(x) for x in umo_scope]:
                return name
            if gid in [s.strip() for s in str(scope.get("users") or "").split(",") if s.strip()]:
                return name

    return scope_default_group(groups)


def _group_of_umo(umo: str) -> str:
    """从 umo 里提取群号/用户号（形如 platform:MessageType:id）。"""
    parts = str(umo).split(":")
    return parts[-1] if parts else ""


def scope_default_group(groups: dict[str, dict]) -> str | None:
    """找出被标记为全局默认的组。"""
    for name, g in (groups or {}).items():
        if g["enabled"] and (g["scope"] or {}).get("default"):
            return name
    return None


# ── 轮询状态 ────────────────────────────────────────────────────────────


async def next_round_robin(members: list[str], group_name: str) -> str | None:
    """按轮询策略取下一个成员，位置存 sp。

    存储 key：``model_group_rr.<组名>``（sp global 作用域）。
    存储读写失败一律退回"取第一个"，不让轮询故障影响正常对话。
    """
    if not members:
        return None
    if len(members) == 1:
        return members[0]
    try:
        from astrbot.api import sp

        key = f"{RR_KEY_PREFIX}{group_name}"
        idx = await sp.global_get(key, 0)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = 0
        picked = members[idx % len(members)]
        await sp.global_put(key, (idx + 1) % len(members))
        return picked
    except Exception as exc:
        logger.warning(f"[model_group] 轮询状态读写失败，退回首个成员：{exc}")
        return members[0]


async def resolve_group_providers(
    group_name: str, config: dict
) -> tuple[list[str], dict]:
    """解析组名 → (provider id 列表, 组定义)。

    ``provider id 列表`` 已按策略给出**建议的主模型顺序**：
    * fallback —— 组内原序，第一个为主
    * round_robin —— 轮询起点已前移到选中项，后续依次为降级候选

    组名不存在或为空时返回空列表（调用方应回退到原provider id）。
    """
    groups = load_groups(config)
    g = groups.get(str(group_name or "").strip())
    if g is None:
        return [], {}
    try:
        members = expand_group(group_name, groups)
    except GroupError as exc:
        logger.error(f"[model_group] {exc}，该组展开失败")
        return [], g

    if not members:
        return [], g

    if g["strategy"] == STRATEGY_ROUND_ROBIN:
        picked = await next_round_robin(members, g["name"])
        if picked:
            i = members.index(picked)
            members = members[i:] + members[:i]
    return members, g