"""模块开关面板 API（``/api/v1/module-switches/*``）。

为「上下文注入管线」等集中式编排插件提供 WebUI 控制面：

    GET  /module-switches/registry   列出可编排的管线及其阶段
    GET  /module-switches/{name}     读取某管线的状态与阶段开关
    PUT  /module-switches/{name}     更新总开关 / 阶段开关
    POST /module-switches/{name}/rebind  重新绑定阶段（插件热重载后用）

设计要点
--------
* 面板需要看到**运行态**（哪个阶段绑上了、上次耗时多少），这些数据只存在于
  插件实例内存中，因此这里直接向插件的 ``get_status()`` 取，而不是自己算。
* 写入走插件自身的配置对象（``AstrBotConfig``），保证与WebUI 插件配置页
  改的是同一份文件，不会出现两处配置打架。
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from astrbot.core import logger
from astrbot.core.config import AstrBotConfig

from .auth import AuthContext, require_dashboard_user

router = APIRouter(prefix="/module-switches", tags=["Module Switches"])


class StageToggle(BaseModel):
    key: str
    enabled: bool


class SwitchUpdate(BaseModel):
    """更新某管线的开关。"""

    total_enabled: bool | None = Field(default=None, description="总开关；None 表示不改")
    stages: list[StageToggle] | None = Field(
        default=None,
        description="阶段开关列表；None 表示不改",
    )


def _find_star(plugin_name: str):
    """在 star_map 中按 plugin_id / name 找插件实例。

    ``star_map`` 是 ``astrbot.core.star.star`` 的模块级全局字典
    （key 为模块路径，value 为 StarMetadata，实例在 ``star_cls``）。
    """
    try:
        from astrbot.core.star.star import star_map
    except Exception:
        return None
    for path, meta in list(star_map.items()):
        pid = getattr(meta, "plugin_id", "") or ""
        name = getattr(meta, "name", "") or ""
        if plugin_name in (pid, name, pid.split("/")[-1], path.split(".")[-1]):
            inst = getattr(meta, "star_cls", None)
            if inst is not None and hasattr(inst, "get_status"):
                return inst
    return None


_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


def _require_plugin_name(plugin_name: str) -> str:
    """校验插件名，阻断 ``../`` 之类的路径穿越。

    ``plugin_name`` 直接来自 URL 路径，会被拼进配置文件名与插件目录路径，
    必须先确认它只是一个普通标识符。

    Args:
        plugin_name: URL 里传入的插件名。

    Returns:
        校验通过的插件名。

    Raises:
        ValueError: 名字为空、含非法字符或为 ``.`` / ``..`` 时抛出。
    """
    name = str(plugin_name or "").strip()
    if not name or name in {".", ".."} or not _PLUGIN_NAME_RE.match(name):
        raise ValueError("非法的插件名")
    return name


def _config_path_for(plugin_name: str):
    from pathlib import Path

    from astrbot.core.utils.astrbot_path import get_astrbot_config_path

    name = _require_plugin_name(plugin_name)
    return Path(str(get_astrbot_config_path())) / f"{name}_config.json"


def _load_cfg(plugin_name: str) -> AstrBotConfig:
    """打开插件配置。必须传 schema，否则 AstrBotConfig 会回退到主配置。"""
    name = _require_plugin_name(plugin_name)
    path = _config_path_for(name)
    schema: dict[str, Any] = {}
    try:
        from pathlib import Path as _P

        from astrbot.core.utils.astrbot_path import get_astrbot_path

        plugin_root = _P(str(get_astrbot_path())) / "data" / "plugins"
        sp = (plugin_root / name / "_conf_schema.json").resolve(strict=False)
        # 双保险：解析后仍须落在 plugins 目录内
        if sp.is_file() and sp.is_relative_to(plugin_root.resolve(strict=False)):
            import json

            schema = json.loads(sp.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"读取 {plugin_name} 的 schema 失败：{exc}")
    return AstrBotConfig(config_path=str(path), schema=schema)


# 已知的可编排管线：名称 -> 展示名。可从插件 metadata 补充，这里给出默认值。
KNOWN_PIPELINES = {
    "astrbot_plugin_context_pipeline": "上下文注入管线",
}


@router.get("/registry")
async def list_pipelines(request: Request, _auth: AuthContext = Depends(require_dashboard_user)):
    """列出支持集中开关的管线。"""
    out = []
    for pname, label in KNOWN_PIPELINES.items():
        inst = _find_star(pname)
        out.append(
            {
                "plugin": pname,
                "label": label,
                "available": inst is not None,
                "stage_count": len((inst.get_status() or {}).get("stages", [])) if inst else 0,
            }
        )
    return {"data": out}


@router.get("/{plugin_name}")
async def get_pipeline_state(
    plugin_name: str,
    request: Request,
    _auth: AuthContext = Depends(require_dashboard_user),
):
    """读取管线运行态 + 阶段开关。"""
    try:
        plugin_name = _require_plugin_name(plugin_name)
    except ValueError:
        return JSONResponse(status_code=400, content={"data": {"error": "非法的插件名"}})
    inst = _find_star(plugin_name)
    if inst is None:
        return JSONResponse(
            status_code=404,
            content={"data": {"error": f"插件未加载或未提供 get_status()：{plugin_name}"}},
        )
    st = inst.get_status() or {}
    return {"data": {"plugin": plugin_name, **st}}


@router.put("/{plugin_name}")
async def update_pipeline(
    plugin_name: str,
    payload: SwitchUpdate,
    request: Request,
    _auth: AuthContext = Depends(require_dashboard_user),
):
    """更新总开关 / 阶段开关并落盘。"""
    try:
        plugin_name = _require_plugin_name(plugin_name)
    except ValueError:
        return JSONResponse(status_code=400, content={"data": {"error": "非法的插件名"}})
    inst = _find_star(plugin_name)
    if inst is None:
        return JSONResponse(
            status_code=404,
            content={"data": {"error": f"插件未加载：{plugin_name}"}},
        )

    cfg = _load_cfg(plugin_name)
    changed: list[str] = []

    if payload.total_enabled is not None:
        if bool(cfg.get("enabled", True)) != bool(payload.total_enabled):
            cfg["enabled"] = bool(payload.total_enabled)
            changed.append("enabled")

    if payload.stages is not None:
        # 内核 schema 不支持嵌套 object，阶段开关用平铺键 stage_<key> 存储。
        # 顺带把旧的嵌套写法（stages: {...}）清掉，避免两份配置并存。
        for t in payload.stages:
            ck = f"stage_{t.key}"
            if cfg.get(ck) != bool(t.enabled):
                cfg[ck] = bool(t.enabled)
                changed.append(ck)
        if isinstance(cfg.get("stages"), dict):
            cfg["stages"] = {}
            changed.append("stages(cleared)")

    if not changed:
        return {"data": {"changed": [], "message": "配置无变化"}}

    try:
        cfg.save_config()
    except Exception as exc:
        # 异常原文可能带配置文件绝对路径，只写日志。
        logger.error("写入 %s 配置失败：%s", plugin_name, exc, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "data": {
                    "changed": [],
                    "message": "落盘失败，请查看服务端日志了解详情",
                }
            },
        )

    # 同步到插件内存（插件把配置存在 self.config 上）
    try:
        inst.config = dict(cfg)
    except Exception:
        pass

    return {"data": {"changed": changed, "message": "已保存"}}


@router.post("/{plugin_name}/rebind")
async def rebind_pipeline(
    plugin_name: str,
    request: Request,
    _auth: AuthContext = Depends(require_dashboard_user),
):
    """重新绑定各阶段（目标插件热重载或启停后使用）。"""
    try:
        plugin_name = _require_plugin_name(plugin_name)
    except ValueError:
        return JSONResponse(status_code=400, content={"data": {"error": "非法的插件名"}})
    inst = _find_star(plugin_name)
    if inst is None or not hasattr(inst, "bind_all"):
        return JSONResponse(
            status_code=404,
            content={"data": {"error": f"插件未加载或不支持重绑：{plugin_name}"}},
        )
    result = inst.bind_all()
    return {"data": {"result": result}}
