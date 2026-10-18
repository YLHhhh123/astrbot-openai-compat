"""清理上下文：用小模型把过长的注入内容重写成精炼版本。

与 ``prompt_compress`` 的区别（两者可同时开启，顺序 850 → 900）：

* ``prompt_compress`` 是**截断**为主，保证确定性的长度上限；
* 本模块是**重写**，保留语义但去掉冗余，适合「内容有价值但太长」的注入块。

只有当 system_prompt 超过 ``min_chars`` 且``model`` 配置了（或解析到模型）
才会真正调用；否则静默跳过，避免每条消息都多烧一次 token。
"""

from __future__ import annotations

from astrbot.api import logger

from .model_resolver import summarize_with_model
from .stages import CGM_END_MARK, CGM_START_MARK, GC_END_MARK, GC_START_MARK

# 默认只清理这几类"可重写"的注入块，其余（如系统设定）保持原样，
# 避免模型改写掉不该动的内容。
DEFAULT_TARGETS = ("group_context", "cross_group_memory")


def _block_bounds(text: str, start_mark: str, end_mark: str) -> list[tuple[int, int]]:
    """找出所有 start/end 标记对，返回 (start_idx, end_idx) 半开区间。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        s = text.find(start_mark, pos)
        if s < 0:
            break
        e = text.find(end_mark, s)
        if e < 0:
            break
        e += len(end_mark)
        spans.append((s, e))
        pos = e
    return spans


def extract_block(text: str, kind: str) -> tuple[int, int, str] | None:
    """取出指定类型的注入块，返回 (起, 止, 内容)。找不到返回 None。"""
    if kind == "group_context":
        start, end = GC_START_MARK, GC_END_MARK
    elif kind == "cross_group_memory":
        start, end = CGM_START_MARK, CGM_END_MARK
    else:
        return None
    spans = _block_bounds(text, start, end)
    if not spans:
        return None
    # 取最长的一块（多块时也只处理第一块，避免超长 prompt 拖垮）
    s, e = max(spans, key=lambda x: x[1] - x[0])
    return s, e, text[s:e]


CLEAN_PROMPT = (
    "请把下面的聊天记录压缩成简洁要点，保留发言者、时间线索、结论与诉求，"
    "合并重复表述，去掉寒暄与无信息量的附和。\n"
    "严格要求：只输出压缩结果，不要解释、不要加标题、不要编造原文没有的信息。\n\n"
    "{content}"
)

CLEAN_SYSTEM = "你是上下文压缩助手。只输出压缩结果正文。"


async def clean_context(
    context,
    req,
    *,
    config: dict,
    event=None,
) -> str:
    """执行清理。就地修改 ``req.system_prompt``，返回一行状态描述。

    失败或跳过时返回以``skipped:`` / ``error:`` 开头的说明，供面板展示。
    """
    sp = getattr(req, "system_prompt", None) or ""
    min_chars = int(config.get("cleanup_min_chars") or 1200)
    if len(sp) < min_chars:
        return f"skipped: 注入内容 {len(sp)} 字 < 阈值 {min_chars}"

    targets = config.get("cleanup_targets") or list(DEFAULT_TARGETS)
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]

    hits = [(k, extract_block(sp, k)) for k in targets]
    hits = [(k, v) for k, v in hits if v]
    if not hits:
        return "skipped: 未找到可清理的注入块"

    model = config.get("cleanup_model") or None
    timeout = float(config.get("cleanup_timeout") or 30.0)
    max_chars = int(config.get("cleanup_max_chars") or 0)

    rewritten = sp
    changed = 0
    for kind, (s, e, block) in hits:
        body = block
        # 标记本身保留，只重写中间内容
        mid_s = block.find("\n")
        mid_e = block.rfind("\n")
        if mid_s >= 0 and mid_e > mid_s:
            prefix, payload, suffix = block[: mid_s + 1], block[mid_s + 1 : mid_e], block[mid_e:]
        else:
            prefix, payload, suffix = "", block, ""

        out = await summarize_with_model(
            context,
            CLEAN_PROMPT.format(content=payload),
            system_prompt=CLEAN_SYSTEM,
            model=model,
            event=event,
            prefer_session=True,
            timeout=timeout,
        )
        if not out:
            logger.debug(f"[context_pipeline] 清理 {kind} 失败（模型无输出），保留原文")
            continue
        if max_chars and len(out) > max_chars:
            out = out[:max_chars].rstrip() + "…"

        new_block = prefix + out + suffix
        if new_block != block:
            # 偏移量会随替换变化，从后往前替换以免影响未处理的区间
            rewritten = rewritten[:s] + new_block + rewritten[e:]
            changed += 1

    if changed:
        req.system_prompt = rewritten
        return f"已重写 {changed} 个注入块，{len(sp)} → {len(rewritten)} 字"
    return "skipped: 清理未产生变化"
