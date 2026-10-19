"""用户形象与多轮思考的 Web API（``/api/v1/persona/*``）。

面板入口。审批类操作复用 dashboard 的登录态（:func:`require_dashboard_user`），
其余只读接口同样需要登录 —— 画像含用户信息，不应匿名可读。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from astrbot.core import logger

from .auth import AuthContext, require_dashboard_user

router = APIRouter(prefix="/persona", tags=["Persona"])

SETTINGS_KEY = "persona_settings"
REFLECT_KEY = "reflection_settings"


def _cfg() -> dict:
    from astrbot.core import astrbot_config

    return dict(astrbot_config or {})


def _models():
    from astrbot.core.persona import models

    return models


def _infer():
    from astrbot.core.persona import infer

    return infer


def _reflect():
    from astrbot.core.persona import reflect

    return reflect


# ── 只读 ────────────────────────────────────────────────────────────────


@router.get("/settings")
async def get_settings(_auth: AuthContext = Depends(require_dashboard_user)):
    """面板主状态：画像开关 + 反思开关。"""
    cfg = _cfg()
    ps = _infer().PersonaSettings.from_config(cfg.get(SETTINGS_KEY))
    rs = _reflect().ReflectionConfig.from_config(cfg.get(REFLECT_KEY))
    return {
        "data": {
            "persona": {
                "enabled": ps.enabled,
                "idle_minutes": ps.idle_minutes,
                "min_samples": ps.min_samples,
                "max_per_day": ps.max_per_day,
                "model_group": ps.model_group,
            },
            "reflection": {
                "enabled": rs.enabled,
                "rounds": rs.rounds,
                "mode": rs.mode,
                "send_interval": rs.send_interval,
                "model_group": rs.model_group,
                "timeout": rs.timeout,
            },
        }
    }


@router.get("/list")
async def list_personas(
    limit: int = 100, _auth: AuthContext = Depends(require_dashboard_user)
):
    """列出已有画像（按更新时间倒序）。"""
    from astrbot.core import db_helper

    rows = await db_helper.get_user_personas()
    rows = sorted(rows, key=lambda r: getattr(r, "updated_at", 0), reverse=True)
    rows = rows[: max(1, min(limit, 500))]
    return {"data": [r.as_dict() for r in rows]}


@router.get("/{user_id}")
async def get_one(
    user_id: str, _auth: AuthContext = Depends(require_dashboard_user)
):
    """取单个用户的画像 + 群修正项。"""
    from astrbot.core import db_helper

    persona = await db_helper.get_user_persona(user_id)
    adjusts = await db_helper.get_persona_group_adjusts(user_id)
    data = persona.as_dict() if persona else None
    return {"data": {"persona": data, "adjusts": [a.as_dict() for a in adjusts]}}


@router.get("/{user_id}/logs")
async def get_logs(
    user_id: str, limit: int = 20, _auth: AuthContext = Depends(require_dashboard_user)
):
    """推理流水（回答"上次为什么没更新"）。"""
    from astrbot.core import db_helper

    rows = await db_helper.get_persona_inference_logs(user_id)
    rows = rows[: max(1, min(limit, 100))]
    return {
        "data": [
            {
                "id": r.id,
                "ok": r.ok,
                "sample_count": r.sample_count,
                "reason": r.reason,
                "trigger": r.trigger,
                "elapsed_ms": r.elapsed_ms,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


# ── 写 ──────────────────────────────────────────────────────────────────


class PersonaSettingsBody(BaseModel):
    enabled: bool | None = None
    idle_minutes: int | None = None
    min_samples: int | None = None
    max_per_day: int | None = None
    model_group: str | None = None


class ReflectionSettingsBody(BaseModel):
    enabled: bool | None = None
    rounds: int | None = None
    mode: str | None = None
    send_interval: float | None = None
    model_group: str | None = None
    timeout: float | None = None


def _apply(cfg: dict, key: str, body: BaseModel) -> dict:
    current = dict(cfg.get(key) or {})
    for field, value in body.model_dump(exclude_none=True).items():
        current[field] = value
    return current


@router.put("/settings")
async def update_settings(
    body: PersonaSettingsBody, _auth: AuthContext = Depends(require_dashboard_user)
):
    """更新画像配置。"""
    from astrbot.core import astrbot_config

    cfg = _cfg()
    merged = _apply(cfg, SETTINGS_KEY, body)
    astrbot_config[SETTINGS_KEY] = merged
    astrbot_config.save_config()
    ps = _infer().PersonaSettings.from_config(merged)
    logger.info(
        f"[persona] 配置更新：enabled={ps.enabled} idle={ps.idle_minutes}min "
        f"min_samples={ps.min_samples}"
    )
    return {"data": {"persona": ps.__dict__}}


@router.put("/reflection")
async def update_reflection(
    body: ReflectionSettingsBody, _auth: AuthContext = Depends(require_dashboard_user)
):
    """更新多轮思考配置。"""
    from astrbot.core import astrbot_config

    cfg = _cfg()
    merged = _apply(cfg, REFLECT_KEY, body)
    astrbot_config[REFLECT_KEY] = merged
    astrbot_config.save_config()
    rs = _reflect().ReflectionConfig.from_config(merged)
    logger.info(
        f"[reflection] 配置更新：enabled={rs.enabled} rounds={rs.rounds} mode={rs.mode}"
    )
    return {"data": {"reflection": rs.__dict__}}


class InferBody(BaseModel):
    user_id: str
    group_id: str | None = None
    trigger: str = "manual"


@router.post("/infer")
async def trigger_infer(
    body: InferBody, _auth: AuthContext = Depends(require_dashboard_user)
):
    """手动触发一次形象推理。"""
    from astrbot.core.persona.runner import run_persona_inference

    result = await run_persona_inference(
        user_id=body.user_id, group_id=body.group_id, trigger=body.trigger
    )
    return {"data": result.as_dict()}


@router.delete("/{user_id}")
async def delete_persona(
    user_id: str, _auth: AuthContext = Depends(require_dashboard_user)
):
    """删除某人的画像。"""
    from astrbot.core import db_helper

    ok1 = await db_helper.delete_user_persona(user_id)
    logger.info(f"[persona] 删除画像：{user_id}")
    return {"data": {"deleted": bool(ok1)}}
