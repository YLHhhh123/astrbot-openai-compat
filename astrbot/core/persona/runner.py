"""推理执行器：把 :mod:`infer` 的纯逻辑接到真实数据库与模型上。

``infer.py`` 保持"纯函数 + 注入 call_model"，便于单测；这里负责取数据、拿
provider、落库。这样测试不需要数据库，也不需要真模型。

模型选择：走**模型分组**（:mod:`astrbot.core.provider.model_groups`）。
分组名填不进就退回当前会话模型 —— 面板上留空表示"用现在这个"，
这是多数情况下的期望。
"""

from __future__ import annotations

import time

from astrbot.core import logger

from .infer import (
    PersonaSettings,
    collect_messages,
    infer_persona,
)
from .models import (
    PersonaInferenceLog,
    UserPersona,
    effective_confidence,
    sufficient,
)

SETTINGS_KEY = "persona_settings"

# 正在推理中的 user_id，防止同一用户并发重复推理
_in_flight: set[str] = set()


def _db():
    from astrbot.core import db_helper

    return db_helper


async def make_model_caller(provider_id: str | None = None):
    """构造一个 ``(prompt, timeout) -> str`` 的模型调用器。

    Args:
        provider_id: 指定的 provider 或**分组名**；为空则用当前默认模型。
    """

    async def call(prompt: str, timeout: float) -> str:
        from astrbot.core import astrbot_config

        from astrbot.core.provider.entities import ProviderType
        from astrbot.core.provider.manager import provider_manager

        target = provider_id or ""
        if not target:
            prov = await provider_manager.get_using_provider_async(
                ProviderType.CHAT_COMPLETION, None
            )
        else:
            # 组名或裸 id 都交给内核解析（model_groups 会识别组名）
            prov = await provider_manager.get_provider_by_id(target)
            if prov is None:
                prov = await provider_manager.resolve_by_model_group_async(
                    ProviderType.CHAT_COMPLETION, None, target
                )
        if prov is None:
            raise RuntimeError(f"找不到可用的模型（指定：{target or '默认'}）")

        async def _do():
            return await prov.text_chat(
                prompt=prompt,
                system_prompt=(
                    "你在分析聊天记录以概括一个人的形象。只输出题目要求的 JSON，"
                    "不要输出任何解释性文字。"
                ),
                contexts=[],
            )

        import asyncio

        return await asyncio.wait_for(_do(), timeout=timeout)

    return call


async def run_persona_inference(
    *,
    user_id: str,
    group_id: str | None = None,
    trigger: str = "manual",
) -> "object":
    """为某个用户跑一次形象推理，并把结果落库。

    失败（样本不足 / 模型不可用 / 输出不可解析）时**保留旧画像不动**。
    """
    from astrbot.core import astrbot_config

    models_mod = __import__(
        "astrbot.core.persona.models", fromlist=["InferenceLike"]
    )
    cfg = PersonaSettings.from_config(astrbot_config.get(SETTINGS_KEY))
    db = _db()

    if user_id in _in_flight:
        logger.debug(f"[persona] {user_id} 正在推理中，跳过")
        return models_mod.InferenceLogResult(
            ok=False, reason="正在推理中", sample_count=0
        )

    _in_flight.add(user_id)
    try:
        rows = await db.get_messages_by_sender(user_id, cfg.max_messages)
        messages = collect_messages(rows, cfg.max_messages)

        previous = await db.get_user_persona(user_id)
        prev_dict = previous.as_dict() if previous else None

        caller = await make_model_caller(cfg.model_group)
        result = await infer_persona(messages, caller, cfg, prev_dict)

        if not result.ok:
            await _log(db, user_id, trigger, result.ok, result.sample_count,
                       result.reason, result.elapsed_ms)
            return result

        row = UserPersona(
            user_id=user_id,
            summary=result.summary,
            traits=result.traits,
            evidence=result.evidence,
            confidence=result.confidence,
            sample_count=result.sample_count,
            persona_model_id=cfg.model_group,
            updated_at=time.time(),
        )
        await db.upsert_user_persona(row)
        await _log(db, user_id, trigger, True, result.sample_count,
                   "", result.elapsed_ms)
        logger.info(
            f"[persona] {user_id} 推理完成：置信度 {result.confidence} "
            f"样本 {result.sample_count}（{trigger}）"
        )
        return result
    except Exception as exc:
        logger.warning(f"[persona] 推理异常：{type(exc).__name__}: {exc}")
        try:
            await _log(db, user_id, trigger, False, 0,
                       f"{type(exc).__name__}: {exc}", 0)
        except Exception:
            pass
        return models_mod.InferenceLogResult(
            ok=False, reason=f"{type(exc).__name__}: {exc}", sample_count=0
        )
    finally:
        _in_flight.discard(user_id)


async def _log(db, user_id, trigger, ok, sample_count, reason, elapsed):
    try:
        await db.insert_persona_inference_log(
            PersonaInferenceLog(
                user_id=user_id,
                trigger=trigger,
                ok=ok,
                sample_count=sample_count,
                reason=reason[:500],
                elapsed_ms=elapsed,
                created_at=time.time(),
            )
        )
    except Exception as exc:
        logger.debug(f"[persona] 写推理日志失败：{exc}")


def in_flight(user_id: str) -> bool:
    """该用户是否正在推理中。"""
    return user_id in _in_flight
