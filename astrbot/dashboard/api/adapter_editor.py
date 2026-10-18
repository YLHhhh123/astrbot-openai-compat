"""适配器编辑面板 API（``/api/v1/adapter-editor/*``）。

审批动作统一走 ``require_tier`` 语义 —— 插件自身的 ``approve_tier``
在面板侧再校验一次（防止有人绕过插件直接打接口）。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from astrbot.core import logger
from astrbot.core.config import AstrBotConfig

from .auth import AuthContext, require_dashboard_user

router = APIRouter(prefix="/adapter-editor", tags=["Adapter Editor"])

PLUGIN = "astrbot_plugin_adapter_editor"
TIER_ORDER = {"normal_admin": 1, "mid_admin": 2, "top_admin": 3}


def _find_plugin():
    try:
        from astrbot.core.star.star import star_map
    except Exception:
        return None
    for _path, meta in list(star_map.items()):
        pid = getattr(meta, "plugin_id", "") or ""
        name = getattr(meta, "name", "") or ""
        if PLUGIN in (pid, name, pid.split("/")[-1]):
            return getattr(meta, "star_cls", None)
    return None


def _import_proposal():
    """从插件包内取proposal 模块（它是纯逻辑，不依赖 astrbot 运行时）。"""
    from astrbot.core.utils.astrbot_path import get_astrbot_path
    import importlib.util
    import os
    import sys

    pkg_dir = os.path.join(
        str(get_astrbot_path()), "data", "plugins", PLUGIN
    )
    init = os.path.join(pkg_dir, "__init__.py")
    if not os.path.isdir(pkg_dir):
        return None
    # 让 data.plugins.<PLUGIN> 成为可导入包
    root = os.path.join(str(get_astrbot_path()), "data", "plugins")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return importlib.import_module(f"data.plugins.{PLUGIN}.proposal")
    except Exception:
        spec = importlib.util.spec_from_file_location(
            f"{PLUGIN}_proposal", os.path.join(pkg_dir, "proposal.py")
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:
            logger.warning(f"[adapter-editor] 载入 proposal 模块失败：{exc}")
            return None
        return mod


def _cfg() -> AstrBotConfig:
    from pathlib import Path

    from astrbot.core.utils.astrbot_path import get_astrbot_config_path

    path = Path(str(get_astrbot_config_path())) / f"{PLUGIN}_config.json"
    schema: dict[str, Any] = {}
    try:
        sp = Path(str(get_astrbot_path())) / "data" / "plugins" / PLUGIN / "_conf_schema.json"
        if sp.is_file():
            import json

            schema = json.loads(sp.read_text(encoding="utf-8"))
    except Exception:
        pass
    return AstrBotConfig(config_path=str(path), schema=schema)


def _approver_tier() -> int:
    return TIER_ORDER.get(str(_cfg().get("approve_tier") or "top_admin"), 3)


def _caller_tier(requester: Request) -> int:
    """取调用者在分级体系里的等级（查 sp 里同步的分级数据）。"""
    try:
        from astrbot.builtin_stars.builtin_commands.tier_api import (
            get_level_by_id,
        )
    except Exception:
        return 0
    uid = str((getattr(requester.state, "user", None) or {}).get("username", ""))
    if not uid:
        try:
            uid = str(requester.state.user_id)
        except Exception:
            uid = ""
    if not uid:
        return 0
    lv = get_level_by_id(uid)
    return {0: 0, 1: 1, 2: 2, 3: 3}.get(int(lv or 0), 0)


def _deny(msg: str):
    return JSONResponse(status_code=403, content={"data": {"error": msg}})


# ── 读 ──────────────────────────────────────────────────────────────────


@router.get("/status")
async def get_status(_auth: AuthContext = Depends(require_dashboard_user)):
    """面板主状态：开关、权限、清单、各状态提案数。"""
    inst = _find_plugin()
    if inst is not None and hasattr(inst, "get_status"):
        return {"data": inst.get_status()}
    # 插件未加载时也要能看配置，避免"面板打不开就没法排查"
    c = _cfg()
    return {
        "data": {
            "enabled": bool(c.get("enabled", False)),
            "approve_tier": str(c.get("approve_tier") or "top_admin"),
            "protected_adapters": str(c.get("protected_adapters") or "").split(","),
            "allow_new_adapter": bool(c.get("allow_new_adapter", True)),
            "quota": {
                "per_session": int(c.get("quota_per_session") or 3),
                "per_day": int(c.get("quota_per_day") or 10),
                "max_diff_lines": int(c.get("quota_max_diff_lines") or 200),
            },
            "counts": {},
            "plugin_loaded": False,
        }
    }


@router.get("/proposals")
async def list_proposals(
    status: str = "",
    _auth: AuthContext = Depends(require_dashboard_user),
):
    """列出提案（不含正文，避免响应过大）。"""
    mod = _import_proposal()
    if mod is None:
        return {"data": []}
    items = mod.list_proposals(status or None)
    out = []
    for p in items:
        d = p.to_dict()
        d.pop("new_content", None)
        out.append(d)
    return {"data": out}


@router.get("/proposals/{pid}")
async def get_proposal(pid: str, _auth: AuthContext = Depends(require_dashboard_user)):
    """取单个提案，含 unified diff。"""
    mod = _import_proposal()
    if mod is None:
        return JSONResponse(
            status_code=404, content={"data": {"error": "proposal 模块不可用"}}
        )
    p = mod.load_proposal(pid)
    if p is None:
        return JSONResponse(
            status_code=404, content={"data": {"error": "提案不存在"}}
        )
    d = p.to_dict()
    d["diff"] = mod.diff_of(p)
    return {"data": d}


# ── 写 ──────────────────────────────────────────────────────────────────


class Toggle(BaseModel):
    enabled: bool


class ProtectList(BaseModel):
    adapters: list[str]


class ApproveBody(BaseModel):
    note: str = ""


@router.put("/switch")
async def set_switch(
    body: Toggle, request: Request, _auth: AuthContext = Depends(require_dashboard_user)
):
    """开关总开关。开启需达到审批等级——避免普通 admin 悄悄打开。"""
    if body.enabled and _caller_tier(request) < _approver_tier():
        return _deny("开启该功能需要审批等级")
    c = _cfg()
    c["enabled"] = bool(body.enabled)
    c.save_config()
    inst = _find_plugin()
    if inst is not None and hasattr(inst, "config"):
        inst.config = dict(c)
    mod = _import_proposal()
    if mod is not None:
        mod.audit("switch", enabled=bool(body.enabled))
    return {"data": {"enabled": bool(body.enabled)}}


@router.put("/protected")
async def set_protected(
    body: ProtectList, request: Request, _auth: AuthContext = Depends(require_dashboard_user)
):
    """设置不可修改的适配器清单。"""
    if _caller_tier(request) < _approver_tier():
        return _deny("修改清单需要审批等级")
    names = [a.strip() for a in body.adapters if a and a.strip()]
    c = _cfg()
    c["protected_adapters"] = ",".join(names)
    c.save_config()
    inst = _find_plugin()
    if inst is not None and hasattr(inst, "config"):
        inst.config = dict(c)
    mod = _import_proposal()
    if mod is not None:
        mod.audit("set_protected", adapters=names)
    return {"data": {"protected_adapters": names}}


@router.post("/proposals/{pid}/approve")
async def approve(
    pid: str, body: ApproveBody, request: Request, _auth: AuthContext = Depends(require_dashboard_user)
):
    """批准提案（不落盘）。"""
    if _caller_tier(request) < _approver_tier():
        return _deny("审批需要达到配置的审批等级")
    mod = _import_proposal()
    if mod is None:
        return JSONResponse(
            status_code=404, content={"data": {"error": "proposal 模块不可用"}}
        )
    uid = str((getattr(request.state, "user", None) or {}).get("username", ""))
    res = mod.approve(pid, uid, body.note)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"data": res})
    return {"data": res}


@router.post("/proposals/{pid}/reject")
async def reject(
    pid: str, body: ApproveBody, request: Request, _auth: AuthContext = Depends(require_dashboard_user)
):
    """驳回提案。"""
    if _caller_tier(request) < _approver_tier():
        return _deny("审批需要达到配置的审批等级")
    mod = _import_proposal()
    if mod is None:
        return JSONResponse(
            status_code=404, content={"data": {"error": "proposal 模块不可用"}}
        )
    uid = str((getattr(request.state, "user", None) or {}).get("username", ""))
    res = mod.reject(pid, uid, body.note)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"data": res})
    return {"data": res}


@router.post("/proposals/{pid}/apply")
async def apply(
    pid: str, request: Request, _auth: AuthContext = Depends(require_dashboard_user)
):
    """把已批准的提案写入真实文件（需审批等级）。

    注意：改完适配器代码需重启 AstrBot 才加载，前端会提示。
    """
    if _caller_tier(request) < _approver_tier():
        return _deny("应用改动需要审批等级")
    mod = _import_proposal()
    if mod is None:
        return JSONResponse(
            status_code=404, content={"data": {"error": "proposal 模块不可用"}}
        )
    uid = str((getattr(request.state, "user", None) or {}).get("username", ""))
    res = mod.apply_to_disk(pid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"data": res})
    res["applied_by"] = uid
    return {"data": res}


@router.post("/proposals/{pid}/rollback")
async def rollback(
    pid: str, request: Request, _auth: AuthContext = Depends(require_dashboard_user)
):
    """把已应用的提案回退到应用前状态。"""
    if _caller_tier(request) < _approver_tier():
        return _deny("回退需要审批等级")
    mod = _import_proposal()
    if mod is None:
        return JSONResponse(
            status_code=404, content={"data": {"error": "proposal 模块不可用"}}
        )
    uid = str((getattr(request.state, "user", None) or {}).get("username", ""))
    res = mod.rollback_applied(pid, uid)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content={"data": res})
    return {"data": res}


@router.get("/audit")
async def audit_trail(lines: int = 100, _auth: AuthContext = Depends(require_dashboard_user)):
    """读审计日志尾部。"""
    mod = _import_proposal()
    if mod is None:
        return {"data": []}
    path = mod.audit_path()
    if not os.path.isfile(path):
        return {"data": []}
    with open(path, encoding="utf-8") as f:
        tail = f.readlines()[-max(1, min(lines, 500)):]
    out = []
    for ln in tail:
        try:
            rec = json.loads(ln)
            rec["time"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(rec.get("ts", 0))
            )
            out.append(rec)
        except Exception:
            continue
    return {"data": out}
