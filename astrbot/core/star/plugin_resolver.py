"""插件名快速解析（模糊匹配）。

后台与内核其它模块经常只有一个**手写、不完整**的插件名，例如
``quote_pool`` / ``quotepool`` / ``引用池`` 都应当能定位到
``astrbot_plugin_quote_pool``。本模块把这类输入解析为唯一的插件注册名。

候选来源是内核实时维护的 ``star_registry``（插件加载、重载、卸载、禁用都会改动它），
因此这里**不做缓存**：新装或改名的插件立刻可被解析到，无需重启、无需手动刷新。

匹配优先级（从高到低，命中即止）：

1. 完整注册名 / ``作者/名字`` 形式的 plugin_id / 目录名 / 展示名
2. 去掉 ``astrbot_plugin_`` 等前缀后的短名
3. 短名前缀匹配
4. 短名子串匹配
5. 分词匹配（``quote_pool`` -> ``quote`` + ``pool`` 都出现在名字里）
6. 子序列匹配（``qpool`` -> ``q...p...o...o...l``）
7. 拼写容错（``difflib`` 相似度，容忍漏字母、错字母）
8. 描述 / 标签 / 展示名的中文或英文子串匹配
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from astrbot.core.star.star import star_registry

# 常见插件名前缀，输入时可以省略
PLUGIN_PREFIXES = ("astrbot_plugin_", "astrbot-", "plugin_", "astrbotplugin")

# 拼写容错的最低相似度，低于它视为不相关
MIN_FUZZY_RATIO = 0.62

# 默认返回的候选数量
DEFAULT_LIMIT = 20

# 第二候选与首候选的分数比达到该阈值时判定为「歧义」（需用户确认）
AMBIGUOUS_RATIO = 0.9

_NON_WORD = re.compile(r"[\s_\-./\\:：，,、|]+")


def _normalize(text: Any) -> str:
    """把文本压成可比较的形式：小写、去掉分隔符与标点。

    Args:
        text: 任意待比较文本。

    Returns:
        归一化后的字符串；``None`` 归一化为空串。
    """
    return _NON_WORD.sub("", str(text or "").lower())


def _strip_prefix(text: str) -> str:
    """去掉常见的插件名前缀（大小写不敏感）。

    Args:
        text: 已小写的插件名或输入。

    Returns:
        去掉前缀后的名字；没有命中任何前缀时原样返回。
    """
    lowered = text.lower()
    for prefix in PLUGIN_PREFIXES:
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def _is_subsequence(needle: str, haystack: str) -> bool:
    """判断 ``needle`` 是否为 ``haystack`` 的子序列。"""
    if not needle:
        return False
    it = iter(haystack)
    return all(ch in it for ch in needle)


@dataclass(frozen=True)
class PluginCandidate:
    """一条插件候选记录（仅包含解析与展示需要的字段）。"""

    name: str
    """插件注册名（内核里的唯一标识，也是 star_map 的键的组成部分）。"""
    plugin_id: str
    """``作者/名字`` 形式，与后台插件列表一致。"""
    root_dir_name: str | None
    """插件目录名，常与注册名相同，但第三方插件可能不同。"""
    display_name: str | None
    """面板展示名（metadata.yaml 的 display_name）。"""
    desc: str | None
    """插件简介。"""
    activated: bool
    """是否已激活（禁用插件仍可被解析，便于重新启用）。"""
    reserved: bool
    """是否为内置保留插件。"""

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接 JSON 序列化的字典。"""
        return {
            "name": self.name,
            "plugin_id": self.plugin_id,
            "root_dir_name": self.root_dir_name,
            "display_name": self.display_name,
            "desc": self.desc,
            "activated": self.activated,
            "reserved": self.reserved,
        }


def iter_candidates() -> list[PluginCandidate]:
    """列出当前全部插件候选。

    每次调用都重新读取 ``star_registry``，因此结果始终反映内核的最新状态
    （新装插件立刻可见、被卸载插件立刻消失）。

    Returns:
        去重后的候选列表，按注册名排序。
    """
    seen: set[str] = set()
    items: list[PluginCandidate] = []
    for meta in list(star_registry):
        name = getattr(meta, "name", None)
        if not name or name in seen:
            continue
        seen.add(name)
        items.append(
            PluginCandidate(
                name=name,
                plugin_id=getattr(meta, "plugin_id", "") or name,
                root_dir_name=getattr(meta, "root_dir_name", None),
                display_name=getattr(meta, "display_name", None),
                desc=getattr(meta, "desc", None),
                activated=bool(getattr(meta, "activated", True)),
                reserved=bool(getattr(meta, "reserved", False)),
            )
        )
    items.sort(key=lambda item: item.name)
    return items


def _score(query: str, candidate: PluginCandidate) -> tuple[float, str] | None:
    """给单个候选打分。

    Args:
        query: 用户原始输入。
        candidate: 待评分候选。

    Returns:
        ``(分数, 命中原因)``；完全不相关时返回 ``None``。
    """
    raw = str(query or "").strip().lower()
    norm = _normalize(raw)
    if not norm:
        return None

    name = candidate.name.lower()
    short = _strip_prefix(name)
    norm_name = _normalize(name)
    norm_short = _normalize(short)
    ids = {
        name,
        short,
        candidate.plugin_id.lower(),
        (candidate.root_dir_name or "").lower(),
        (candidate.display_name or "").lower(),
    }
    if raw in ids:
        return 1000.0, "exact"
    if norm in (norm_name, norm_short):
        return 980.0, "exact"

    # 短名前缀：quote -> astrbot_plugin_quote_pool
    if norm_short.startswith(norm):
        return 900.0 - min(len(norm_short) - len(norm), 60), "prefix"
    # 短名子串：pool -> astrbot_plugin_quote_pool
    pos = norm_short.find(norm)
    if pos >= 0:
        return 820.0 - pos, "substring"
    # 分词：quote pool / quote-pool 都命中
    tokens = [t for t in re.split(r"[\s_\-./]+", raw) if t]
    if len(tokens) > 1 and all(token in norm_name for token in tokens):
        return 760.0, "tokens"
    # 子序列：qpool -> quote_pool
    if len(norm) >= 3 and _is_subsequence(norm, norm_short):
        return 700.0 - max(len(norm_short) - len(norm), 0), "subsequence"
    # 拼写容错：quotepol -> quote_pool
    ratio = SequenceMatcher(None, norm, norm_short).ratio()
    if ratio >= MIN_FUZZY_RATIO:
        return 560.0 * ratio, "fuzzy"
    # 描述 / 展示名命中（中文名通常只写在 desc 或 display_name 里）
    haystack = _normalize(
        " ".join(
            filter(
                None,
                [
                    candidate.display_name,
                    candidate.desc,
                    candidate.plugin_id,
                ],
            )
        )
    )
    if norm and norm in haystack:
        return 420.0, "description"
    if len(tokens) > 1 and all(token in haystack for token in tokens):
        return 400.0, "description-tokens"
    return None


def search(query: str, limit: int = DEFAULT_LIMIT) -> list[dict[str, Any]]:
    """按模糊输入搜索插件。

    Args:
        query: 用户输入（可以是不完整的注册名、短名、目录名或中文描述片段）。
        limit: 最多返回的候选数量。

    Returns:
        按相关度降序排列的候选字典列表，每项含 ``score`` 与 ``match``（命中原因）。
    """
    text = str(query or "").strip()
    if not text:
        return []
    scored: list[tuple[float, str, PluginCandidate]] = []
    for candidate in iter_candidates():
        result = _score(text, candidate)
        if result is None:
            continue
        score, match = result
        scored.append((score, match, candidate))
    # 同分时优先已激活、再按名字长度（越接近输入越可能正确）
    scored.sort(key=lambda item: (-item[0], not item[2].activated, len(item[2].name)))
    capped = max(1, min(int(limit or DEFAULT_LIMIT), 200))
    return [
        {**candidate.to_dict(), "score": round(score, 2), "match": match}
        for score, match, candidate in scored[:capped]
    ]


def resolve(query: str) -> dict[str, Any]:
    """把输入解析为唯一的插件注册名。

    Args:
        query: 用户输入。

    Returns:
        含 ``matched`` / ``name`` / ``ambiguous`` / ``candidates`` 的字典。
        ``matched`` 为 ``False`` 时表示没有候选；``ambiguous`` 为 ``True`` 时
        ``name`` 仍给出最相关的一个，调用方可据此提示用户确认。
    """
    candidates = search(query, limit=10)
    if not candidates:
        return {"matched": False, "ambiguous": False, "name": None, "candidates": []}
    top = candidates[0]
    # 第二名与第一名分数接近（相差不到 10%）时视为歧义：
    # 例如 "quote" 同时命中 quote_pool / quote_timer，调用方应提示用户确认。
    ambiguous = (
        len(candidates) > 1
        and candidates[1]["name"] != top["name"]
        and candidates[1]["score"] >= top["score"] * AMBIGUOUS_RATIO
    )
    return {
        "matched": True,
        "ambiguous": ambiguous,
        "name": top["name"],
        "match": top["match"],
        "score": top["score"],
        "candidates": candidates,
    }


def names_of(queries: list[Any]) -> tuple[list[str], list[str]]:
    """把一批手写插件名批量解析为注册名。

    供「配置里存的可能是简写」的场景使用：先解析再落库，避免后续匹配不上。

    Args:
        queries: 手写名称列表。

    Returns:
        ``(解析成功的注册名列表, 解析失败的原始输入列表)``，两者都保持输入顺序并去重。
    """
    resolved: list[str] = []
    failed: list[str] = []
    for item in queries or []:
        text = str(item or "").strip()
        if not text:
            continue
        result = resolve(text)
        name = result.get("name") if result.get("matched") else None
        if name:
            if name not in resolved:
                resolved.append(name)
        elif text not in failed:
            failed.append(text)
    return resolved, failed
