"""插件类别与入口策略管理 API（``/api/v1/plugin-categories/*``）。

供 Dashboard 设置「功能性 / 互动性」分类，以及配置各入口允许的插件类别。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from astrbot.dashboard.services.plugin_category_service import PluginCategoryService

from .auth import require_dashboard_user

router = APIRouter(
    prefix="/plugin-categories",
    tags=["Plugin Categories"],
    dependencies=[Depends(require_dashboard_user)],
)


def get_service(request: Request) -> PluginCategoryService:
    return request.app.state.services.plugin_categories


@router.get("/plugins")
async def list_plugins(request: Request):
    """列出全部插件的类别判定结果（含来源：override / metadata / default）。"""
    return {"data": await get_service(request).list_plugins()}


@router.get("/meta")
async def get_meta(request: Request):
    """类别枚举、默认值、默认入口策略。"""
    return {"data": await get_service(request).get_meta()}


class SetCategoryRequest(BaseModel):
    category: str


@router.patch("/plugins/{plugin_name}")
async def set_plugin_category(
    plugin_name: str,
    payload: SetCategoryRequest,
    request: Request,
):
    """设置某个插件的类别（写入内核覆盖表）。"""
    category = await get_service(request).set_plugin_category(
        plugin_name, payload.category
    )
    return {"data": {"name": plugin_name, "category": category}}


@router.delete("/plugins/{plugin_name}")
async def clear_plugin_category(plugin_name: str, request: Request):
    """清除覆盖，回落到插件元数据声明或默认值。"""
    cleared = await get_service(request).clear_plugin_category(plugin_name)
    return {"data": {"name": plugin_name, "cleared": cleared}}


@router.get("/entry-policy")
async def get_entry_policy(request: Request):
    """读取入口策略（入口 → 允许的类别列表）。"""
    return {"data": await get_service(request).get_entry_policy()}


class EntryPolicyRequest(BaseModel):
    policy: dict[str, list[str]]


@router.put("/entry-policy")
async def set_entry_policy(payload: EntryPolicyRequest, request: Request):
    """整体写入入口策略。"""
    return {"data": await get_service(request).set_entry_policy(payload.policy)}
