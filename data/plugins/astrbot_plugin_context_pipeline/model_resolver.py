"""模型解析：留空时用默认会话模型，再退回provider 列表第一个。

管线的「清理上下文」与「检查输出」都要额外调一次 LLM，因此需要一个
统一的模型选择入口，用户可在配置里指定或留空。

优先级：

1. 配置里显式写的 ``model``（形如 ``provider_id`` 或 ``provider_id/model``）；
2. 当前会话正在用的模型（``get_current_chat_provider_id(umo)``）——
   这样同一会话里清理与生成用同一款模型，风格一致；
3. provider 列表第一个（``get_all_providers()``）。

选2 还是 3 由参数 ``prefer_session`` 控制：清理上下文时用会话模型更连贯，
输出检查属于「后台校验」，用列表第一个更省资源。
"""

from __future__ import annotations

from astrbot.api import logger


async def resolve_model(
    context,
    configured: str | None,
    *,
    event=None,
    prefer_session: bool = True,
) -> str | None:
    """解析要使用的 chat provider id。

    Args:
        context: AstrBot Context。
        configured: 配置里写的模型 id；留空/None 走自动解析。
        event: 当前事件（可选），用于取会话模型。
        prefer_session: 是否优先用会话当前模型。

    Returns:
        provider id；都拿不到时返回 None。
    """
    cfg = (configured or "").strip()
    if cfg:
        # 允许写 "provider_id/model_name"，只取前半段
        return cfg.split("/", 1)[0] if "/" in cfg else cfg

    if prefer_session and event is not None:
        umo = getattr(event, "unified_msg_origin", None)
        if umo:
            try:
                pid = await context.get_current_chat_provider_id(umo)
                if pid:
                    return str(pid)
            except Exception as exc:
                logger.debug(f"[context_pipeline] 取会话模型失败：{exc}")

    try:
        provs = context.get_all_providers()
        if provs:
            return provs[0].meta().id
    except Exception as exc:
        logger.debug(f"[context_pipeline] 取 provider 列表失败：{exc}")

    return None


async def summarize_with_model(
    context,
    prompt: str,
    *,
    system_prompt: str = "",
    model: str | None = None,
    event=None,
    prefer_session: bool = True,
    timeout: float = 30.0,
    max_tokens: int | None = None,
) -> str | None:
    """用指定（��自动解析的）模型跑一次生成，返回文本；失败返回 None。

    这是给「清理上下文」与「检查输出」共用的一次性调用封装：
    超时、空返回、异常都归一为 None，让调用方决定降级策略。
    """
    import asyncio

    pid = await resolve_model(context, model, event=event, prefer_session=prefer_session)
    if not pid:
        logger.debug("[context_pipeline] 未找到可用模型，跳过本次生成")
        return None

    kwargs: dict = {}
    if max_tokens:
        kwargs["max_tokens"] = max_tokens

    try:
        resp = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=pid,
                prompt=prompt,
                system_prompt=system_prompt,
                **kwargs,
            ),
            timeout=timeout,
        )
    except Exception as exc:
        logger.warning(f"[context_pipeline] 模型 {pid} 调用失败：{exc!r}")
        return None

    text = (getattr(resp, "completion_text", "") or "").strip()
    return text or None
