"""插件快速选中与内测准入 API（``/api/v1/plugin-beta/*``）。

两部分能力：

1. **快速选中**：``GET /plugins`` 与 ``GET /resolve`` 把不完整的输入
   （``quote_pool`` / ``quotepool`` / ``引用池``）解析成插件注册名。
   候选直接来自内核 ``star_registry``，插件增删改后**自动更新**，无需缓存刷新。
2. **内测准入**：读写内测插件名单、白名单用户、一次性内测码与兑换记录。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from astrbot.core.star.beta_gate import ALL_PLUGINS, BetaGate
from astrbot.core.star.plugin_resolver import iter_candidates, resolve, search

from .auth import require_dashboard_user

router = APIRouter(
    prefix="/plugin-beta",
    tags=["Plugin Beta"],
    dependencies=[Depends(require_dashboard_user)],
)


@router.get("/plugins")
async def list_plugin_candidates(
    q: str = Query(default="", description="模糊输入；留空返回全部插件"),
    limit: int = Query(default=50, ge=1, le=500),
):
    """列出插件候选，支持模糊搜索。

    Args:
        q: 模糊输入，留空时返回全部插件（按注册名排序）。
        limit: 最多返回数量。

    Returns:
        候选列表；模糊搜索时附带 ``score`` 与 ``match`` 命中原因。
    """
    if not str(q or "").strip():
        items = [candidate.to_dict() for candidate in iter_candidates()]
        return {"data": {"items": items[:limit], "total": len(items)}}
    items = search(q, limit=limit)
    return {"data": {"items": items, "total": len(items)}}


@router.get("/resolve")
async def resolve_plugin(
    q: str = Query(..., description="不完整的插件名 / 中文描述 / 目录名"),
):
    """把不完整的插件名解析为唯一注册名。"""
    return {"data": resolve(q)}


class BetaConfigRequest(BaseModel):
    """更新内测配置（只提交需要改的字段）。"""

    enabled: bool | None = Field(default=None)
    plugins: list[str] | None = Field(
        default=None, description="内测插件名，支持简写，保存时自动解析为注册名"
    )
    user_ids: list[str] | None = Field(default=None, description="白名单用户 ID")
    admin_bypass: bool | None = Field(default=None, description="管理员是否自动放行")
    block_message: str | None = Field(default=None, description="未授权时的拦截提示")
    redeem_success_message: str | None = Field(
        default=None, description="兑换成功后的提示"
    )


@router.get("/config")
async def get_beta_config():
    """读取内测配置、内测码与兑换记录。"""
    config = await BetaGate.get_config()
    return {
        "data": {
            "config": config,
            "keys": await BetaGate.list_keys(),
            "grants": await BetaGate.list_grants(),
            "wildcard": ALL_PLUGINS,
        }
    }


@router.put("/config")
async def update_beta_config(payload: BetaConfigRequest):
    """按字段更新内测配置，即时生效（无需重启）。"""
    patch: dict[str, Any] = payload.model_dump(exclude_none=True)
    config = await BetaGate.set_config(patch)
    return {"data": {"config": config}}


class CreateKeyRequest(BaseModel):
    """生成一次性内测码。"""

    count: int = Field(default=1, ge=1, le=50)
    scope: str = Field(default=ALL_PLUGINS, description="限定的插件名，* 表示全部")
    note: str = Field(default="", description="备注，便于辨认发放对象")
    expires_in_days: float = Field(default=0, ge=0, description="有效天数，0 为永久")


@router.post("/keys")
async def create_beta_keys(payload: CreateKeyRequest):
    """批量生成一次性内测码（明文仅在本次响应返回）。"""
    keys = await BetaGate.create_keys(
        count=payload.count,
        scope=payload.scope,
        note=payload.note,
        expires_in_days=payload.expires_in_days,
    )
    return {"data": {"keys": keys}}


@router.delete("/keys")
async def delete_beta_key(key: str = Query(..., description="内测码")):
    """删除内测码，并回收它授予的访问权。"""
    return {"data": {"deleted": await BetaGate.delete_key(key)}}


@router.delete("/grants")
async def delete_beta_grant(
    uid: str = Query(..., description="用户 ID"),
    key: str | None = Query(default=None, description="只撤销该内测码授予的权限"),
):
    """撤销某个用户通过内测码获得的资格。"""
    return {"data": {"revoked": await BetaGate.revoke(uid, key=key)}}


@router.get("/check")
async def check_beta_access(
    uid: str = Query(..., description="用户 ID"),
):
    """查询某个用户当前可用的内测插件（排查用）。"""
    config = await BetaGate.get_config()
    grants = await BetaGate.list_grants()
    grant = grants.get(uid) or {}
    granted = list(grant.get("plugins") or [])
    allowed_all = ALL_PLUGINS in granted
    return {
        "data": {
            "uid": uid,
            "enabled": config["enabled"],
            "beta_plugins": config["plugins"],
            "granted": granted,
            "allowed_all": allowed_all,
            "whitelisted": uid in set(config["user_ids"]),
        }
    }
