"""三级超管（tier admin）配置管理 API。

供 WebUI 的「管理员等级」页面使用：读写内置插件 ``builtin_commands`` 的
分级配置（``data/config/builtin_commands_config.json``），并把当前生效的
分级同步到 sp，供 ``tier_api`` 查询。

配置项分三类：
    1. 三级超管 ID 名单：``top_admin_ids`` / ``mid_admin_ids`` / ``normal_admin_ids``
    2. 每级指令白名单：``<role>_commands``（留空=全部，"禁用"=全部禁止）
    3. 每级禁止插件：``<role>_forbidden_plugins``
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from astrbot.core import logger
from astrbot.core.config import AstrBotConfig
from astrbot.core.utils.astrbot_path import get_astrbot_config_path

from .auth import AuthContext, require_dashboard_user

router = APIRouter(prefix="/tier-admin", tags=["Tier Admin"])

_CONFIG_FILE = "builtin_commands_config.json"

TIER_LEVELS = [
    {"value": "top_admin", "label": "最高超管", "rank": 5},
    {"value": "mid_admin", "label": "中级超管", "rank": 4},
    {"value": "normal_admin", "label": "普通超管", "rank": 3},
    {"value": "group_owner", "label": "群主", "rank": 2},
    {"value": "group_admin", "label": "群聊管理员", "rank": 1},
    {"value": "member", "label": "普通成员", "rank": 0},
]

_ID_KEYS = ("top_admin_ids", "mid_admin_ids", "normal_admin_ids")

# 指令白名单 / 禁止插件涉及的层级（超管三级 + 群角色三级）
_SCOPE_ROLES = [t["value"] for t in TIER_LEVELS]


def _split(raw: Any) -> list[str]:
    text = str(raw or "").replace("，", ",").replace("；", ";")
    return [s.strip() for s in text.split(",") if s.strip()]


def _config_file_path():
    """内置插件配置文件绝对路径（``get_astrbot_config_path`` 返回字符串）。"""
    from pathlib import Path

    return Path(str(get_astrbot_config_path())) / _CONFIG_FILE


def _load_config() -> AstrBotConfig:
    """打开内置插件的配置文件。

    ⚠️ 必须传 ``schema``：``AstrBotConfig`` 在无 schema 时会**回退到主配置
    ``cmd_config.json``**，导致误写整个主配置。schema 只用于确定「这是插件配置」
    并补齐缺省键，即使读取失败也必须传空 dict 兜底。
    """
    config_path = _config_file_path()
    try:
        return AstrBotConfig(
            config_path=str(config_path),
            schema=_load_schema(),
        )
    except Exception as exc:
        logger.warning(f"读取 {_CONFIG_FILE} 失败，使用空 schema：{exc}")
        return AstrBotConfig(config_path=str(config_path), schema={})


def _load_schema() -> dict:
    """读取内置插件的 ``_conf_schema.json``（用于配置校验与缺省值补齐）。"""
    try:
        from pathlib import Path

        from astrbot.core.utils.astrbot_path import get_astrbot_path

        schema_path = (
            Path(str(get_astrbot_path()))
            / "astrbot"
            / "builtin_stars"
            / "builtin_commands"
            / "_conf_schema.json"
        )
        with open(schema_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        logger.warning(f"读取 tier 配置 schema 失败：{exc}")
        return {}


async def _sync_sp() -> None:
    """把分级同步到 sp，供其他模块通过 tier_api 查询。"""
    try:
        from astrbot.builtin_stars.builtin_commands.tier_api import sync_tier_ids

        await sync_tier_ids(dict(_load_config()))
    except Exception as exc:  # pragma: no cover - 同步失败不阻塞配置写入
        logger.warning(f"同步超管分级到 sp 失败：{exc}")


def _effective_roles(sender: str) -> list[str]:
    """按当前配置解析某个用户ID 的层级，供 UI 展示参考。"""
    cfg = _load_config()
    from astrbot.builtin_stars.builtin_commands.tier_api import extract_uid

    uid = extract_uid(sender)
    for key in _ID_KEYS:
        if any(extract_uid(item) == uid for item in _split(cfg.get(key))):
            return [key.removesuffix("_ids")]
    return ["member"]


class TierAdminUpdateRequest(BaseModel):
    """更新三级超管配置。"""

    top_admin_ids: str | None = Field(default=None)
    mid_admin_ids: str | None = Field(default=None)
    normal_admin_ids: str | None = Field(default=None)
    commands: dict[str, str] | None = Field(
        default=None,
        description="层级 -> 指令白名单原文（留空=全部，'禁用'=全部禁止）",
    )
    forbidden_plugins: dict[str, str] | None = Field(
        default=None,
        description="层级 -> 禁止插件名，逗号分隔",
    )


@router.get("/config")
async def get_tier_config(
    _auth: AuthContext = Depends(require_dashboard_user),
):
    """读取当前三级超管配置，并附带每个层级的有效指令范围。"""
    cfg = _load_config()
    data: dict[str, Any] = {
        "config_file": _CONFIG_FILE,
        "tier_levels": TIER_LEVELS,
        "id_lists": {key: _split(cfg.get(key)) for key in _ID_KEYS},
        "commands": {role: str(cfg.get(f"{role}_commands") or "") for role in _SCOPE_ROLES},
        "forbidden_plugins": {
            role: _split(cfg.get(f"{role}_forbidden_plugins")) for role in _SCOPE_ROLES
        },
    }
    return {"data": data}


@router.put("/config")
async def update_tier_config(
    payload: TierAdminUpdateRequest,
    _auth: AuthContext = Depends(require_dashboard_user),
):
    """写入三级超管配置并落盘，同步到 sp。"""
    body = payload.model_dump(exclude_none=True)
    cfg = _load_config()

    changed: list[str] = []
    for key in _ID_KEYS:
        if key in body:
            new_value = str(body[key] or "")
            # 规范化：去空白、去重
            normalized = ",".join(dict.fromkeys(_split(new_value)))
            if str(cfg.get(key) or "") != normalized:
                cfg[key] = normalized
                changed.append(key)

    commands = body.get("commands")
    if isinstance(commands, dict):
        for role, value in commands.items():
            if role in _SCOPE_ROLES:
                key = f"{role}_commands"
                if str(cfg.get(key) or "") != str(value or ""):
                    cfg[key] = str(value or "")
                    changed.append(key)

    forbidden = body.get("forbidden_plugins")
    if isinstance(forbidden, dict):
        for role, value in forbidden.items():
            if role in _SCOPE_ROLES:
                key = f"{role}_forbidden_plugins"
                if isinstance(value, list):
                    text = ",".join(str(v).strip() for v in value if str(v).strip())
                else:
                    text = ",".join(_split(value))
                if str(cfg.get(key) or "") != text:
                    cfg[key] = text
                    changed.append(key)

    if not changed:
        return {"data": {"changed": [], "message": "配置无变化"}}

    try:
        cfg.save_config()
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={"data": {"changed": [], "message": f"落盘失败：{exc}"}},
        )

    await _sync_sp()
    return {"data": {"changed": changed, "message": "已保存"}}

@router.get("/resolve")
async def resolve_tier(
    user_id: str,
    _auth: AuthContext = Depends(require_dashboard_user),
):
    """查询某个用户 ID 当前被判定的层级（UI 排查用）。"""
    return {"data": {"user_id": user_id, "roles": _effective_roles(user_id)}}
