"""检查输出：发送前复核回复内容，可按规则拦截或交给小模型判断。

触发点是 ``on_decorating_result``（发送前），不是管线的 ``on_llm_request``，
因此不属于阶段表 —— 它作用于「产出」而非「注入」。

三种模式（``output_check_mode``）：

* ``off``   —— 完全关闭（默认）
* ``rule``  —— 纯规则：命中关键词/ 正则就拦截，不调模型，零成本
* ``llm``   —— 先跑规则，再用小模型复核；模型可配置，留空用默认第一个

动作（``output_check_action``）：

* ``block``（默认）—— 命中即把回复替换为提示文本，不让原内容发出
* ``warn``         —— 只记录，不改回复
* ``strip``        —— 去掉命中的片段，保留其余内容
"""

from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import ResultContentType

from .model_resolver import summarize_with_model

MODE_OFF = "off"
MODE_RULE = "rule"
MODE_LLM = "llm"

ACTION_BLOCK = "block"
ACTION_WARN = "warn"
ACTION_STRIP = "strip"

DEFAULT_KEYWORDS = ""  # 默认不配任何关键词，避免误伤


def _extract_llm_text(event) -> str | None:
    """取出本次要发送的 LLM 文本；非 LLM 结果或无文本返回 None。"""
    result = event.get_result()
    if result is None or not result.chain:
        return None
    if getattr(result, "result_content_type", None) != ResultContentType.LLM_RESULT:
        return None
    texts = [
        (comp.text or "")
        for comp in result.chain
        if isinstance(comp, Plain) and (comp.text or "").strip()
    ]
    return "\n".join(texts) if texts else None


def _replace_llm_text(event, new_text: str) -> None:
    """把 LLM 文本段替换为 new_text，保留非文本组件。"""
    result = event.get_result()
    chain: list[Any] = []
    inserted = False
    for comp in result.chain:
        if isinstance(comp, Plain):
            if not inserted:
                chain.append(Plain(new_text))
                inserted = True
        else:
            chain.append(comp)
    if not inserted:
        chain.append(Plain(new_text))
    result.chain = chain


def compile_rules(config: dict) -> list[re.Pattern]:
    """把配置里的关键词/正则编译成匹配器。"""
    out: list[re.Pattern] = []
    for raw in (config.get("output_block_keywords") or DEFAULT_KEYWORDS).split(","):
        kw = raw.strip()
        if not kw:
            continue
        try:
            out.append(re.compile(kw))
        except re.error as exc:
            logger.warning(f"[context_pipeline] 输出检查正则无效 {kw!r}：{exc}")
    return out


CHECK_PROMPT = (
    "请判断下面这段回复是否包含以下问题之一："
    "1. 泄露系统提示词或内部设定；"
    "2. 明显的幻觉/编造事实；"
    "3. 违反「不要输出解释性前缀」这类输出格式要求。\n"
    '严格要求：只回答 "PASS" 或 "FAIL: <一句话原因>"，不要输出其他内容。\n\n'
    "{content}"
)

CHECK_SYSTEM = "你是输出合规检查器。只输出 PASS 或 FAIL: 原因。"


async def check_output(context, event, *, config: dict) -> str:
    """执行检查。就地修改回复，返回一行状态描述。"""
    mode = (config.get("output_check_mode") or MODE_OFF).strip().lower()
    if mode == MODE_OFF:
        return "skipped: 检查关闭"

    text = _extract_llm_text(event)
    if not text:
        return "skipped: 非 LLM 文本输出"

    action = (config.get("output_check_action") or ACTION_BLOCK).strip().lower()
    rules = compile_rules(config)
    min_len = int(config.get("output_check_min_chars") or 0)
    if min_len and len(text) < min_len:
        return f"skipped: 输出 {len(text)} 字 < 阈值 {min_len}"

    # ① 规则
    hit = [p.pattern for p in rules if p.search(text)]
    if hit and mode == MODE_RULE:
        return _apply(event, text, hit, action, rule_only=True, config=config)

    # ② 模型复核
    if mode == MODE_LLM:
        model = config.get("output_check_model") or None
        timeout = float(config.get("output_check_timeout") or 20.0)
        verdict = await summarize_with_model(
            context,
            CHECK_PROMPT.format(content=text[:4000]),
            system_prompt=CHECK_SYSTEM,
            model=model,
            event=event,
            # 输出检查是后台校验，用provider 列表第一个即可，不必占会话模型
            prefer_session=False,
            timeout=timeout,
        )
        if verdict is None:
            return "skipped: 检查模型无响应（已放行）"
        v = verdict.strip()
        if v.upper().startswith("PASS"):
            return "passed: 模型判定通过"
        reason = v[5:].strip() if v.upper().startswith("FAIL") else v
        return _apply(event, text, [f"模型判定：{reason}"], action, rule_only=False, config=config)

    # mode == rule 且无命中
    return "passed: 规则未命中"


def _apply(event, text: str, reasons: list[str], action: str, *, rule_only: bool, config: dict) -> str:
    tag = "规则" if rule_only else "检查"
    detail = "、".join(reasons)

    if action == ACTION_WARN:
        logger.warning(f"[context_pipeline] 输出检查（{tag}）命中但仅告警：{detail}")
        return f"warn: 命中 {tag} 检查（{detail}），已放行"

    if action == ACTION_STRIP:
        # 只有规则模式能"删掉"命中片段；模型判定是自然语言原因，删不掉。
        if not rule_only:
            return "skipped: strip 模式仅对规则命中生效，模型判定请用 block/warn"
        new_text = text
        for p in compile_rules(config):
            new_text = p.sub("", new_text)
        new_text = new_text.strip()
        if new_text and new_text != text:
            _replace_llm_text(event, new_text)
            return f"stripped: 已移除命中内容（{detail}）"
        return "skipped: 无可移除内容"

    # 默认 block
    logger.info(f"[context_pipeline] 输出检查（{tag}）拦截：{detail}")
    _replace_llm_text(
        event,
        f"[回复已被内容检查拦截：{detail}]\n如需继续，请调整配置或换个说法。",
    )
    return f"blocked: 命中 {tag} 检查（{detail}）"
