"""内测（beta）准入网关。

把若干插件标记为「内测插件」后，只有满足下列条件之一的用户才能使用它们：

1. 出现在白名单用户 ID 列表中（后台手动填写）
2. 兑换过一次性内测 key（key 可限定单个插件，也可放开全部内测插件）
3. 管理员（可配置是否放行）

未获授权的用户触发内测插件的**命令**时，内核拦截该命令并回复自定义提示语；
内测插件的被动钩子与 LLM 工具则**静默**跳过（避免每条消息都刷提示）。

状态全部存在 AstrBot 的 sp 全局存储中，改动即时生效、无需重启：

===================  ==========================================
键                    内容
===================  ==========================================
``beta_gate.config``  ``{enabled, plugins, user_ids, admin_bypass, block_message,
                       redeem_success_message}``
``beta_gate.keys``    ``{key: {scope, note, created_at, expires_at, used_by, used_at}}``
``beta_gate.grants``  ``{uid: {plugins, source, at}}``
===================  ==========================================
"""

from __future__ import annotations

import secrets
import string
import time
from typing import Any

from astrbot.api import logger, sp
from astrbot.core.star.plugin_resolver import names_of

KEY_CONFIG = "beta_gate.config"
KEY_KEYS = "beta_gate.keys"
KEY_GRANTS = "beta_gate.grants"

# 通配符：key / 授权覆盖全部内测插件
ALL_PLUGINS = "*"

DEFAULT_BLOCK_MESSAGE = (
    "该功能正在内测中，暂未对你开放。\n如已获得内测资格，请发送 /内测 <内测码> 激活。"
)

DEFAULT_REDEEM_SUCCESS = "内测资格已激活，现在可以使用该功能了。"

# 一次性 key 的字符集：去掉容易混淆的 0/O/1/I
_KEY_ALPHABET = "".join(
    ch for ch in (string.ascii_uppercase + string.digits) if ch not in "0O1I"
)
_KEY_PREFIX = "HT-"


def _now() -> float:
    """当前时间戳（秒）。"""
    return time.time()


def generate_key() -> str:
    """生成一个一次性内测 key，形如 ``HT-7K3M-Q9XP``。"""
    body = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(8))
    return f"{_KEY_PREFIX}{body[:4]}-{body[4:]}"


def normalize_key(raw: Any) -> str:
    """规范化用户输入的 key（去空格、转大写）。"""
    return str(raw or "").strip().upper().replace(" ", "")


def _as_str_list(value: Any) -> list[str]:
    """把任意配置值收敛成去重后的字符串列表。"""
    if isinstance(value, str):
        items = [part.strip() for part in value.replace("，", ",").split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value]
    else:
        return []
    return list(dict.fromkeys(item for item in items if item))


class BetaGate:
    """内测准入网关（全部方法为静态方法，无需实例化）。"""

    # ------------------------------------------------------------------ 配置读写

    @staticmethod
    async def get_config() -> dict[str, Any]:
        """读取内测配置（缺省值在此统一补齐）。"""
        raw = await sp.global_get(KEY_CONFIG, {})
        data = raw if isinstance(raw, dict) else {}
        return {
            "enabled": bool(data.get("enabled", False)),
            "plugins": _as_str_list(data.get("plugins")),
            "user_ids": _as_str_list(data.get("user_ids")),
            "admin_bypass": bool(data.get("admin_bypass", True)),
            "block_message": str(data.get("block_message") or DEFAULT_BLOCK_MESSAGE),
            "redeem_success_message": str(
                data.get("redeem_success_message") or DEFAULT_REDEEM_SUCCESS
            ),
        }

    @staticmethod
    async def set_config(patch: dict[str, Any]) -> dict[str, Any]:
        """按字段更新内测配置，返回更新后的完整配置。

        Args:
            patch: 待更新字段；只处理已知键，未知键忽略。

        Returns:
            更新后的完整配置字典。
        """
        current = await BetaGate.get_config()
        if not isinstance(patch, dict):
            return current
        if "enabled" in patch:
            current["enabled"] = bool(patch["enabled"])
        if "admin_bypass" in patch:
            current["admin_bypass"] = bool(patch["admin_bypass"])
        if "block_message" in patch:
            text = str(patch.get("block_message") or "").strip()
            current["block_message"] = text or DEFAULT_BLOCK_MESSAGE
        if "redeem_success_message" in patch:
            text = str(patch.get("redeem_success_message") or "").strip()
            current["redeem_success_message"] = text or DEFAULT_REDEEM_SUCCESS
        if "user_ids" in patch:
            current["user_ids"] = _as_str_list(patch.get("user_ids"))
        if "plugins" in patch:
            raw = _as_str_list(patch.get("plugins"))
            # 存库前把简写解析成注册名，避免后续匹配不上；只有解析失败的名字
            # 才保留原文（插件可能尚未安装，装上后自动生效）。
            resolved, failed = names_of(raw)
            current["plugins"] = list(dict.fromkeys(resolved + failed))
        await sp.global_put(KEY_CONFIG, current)
        return current

    # ------------------------------------------------------------------ 一次性 key

    @staticmethod
    async def list_keys() -> dict[str, dict[str, Any]]:
        """列出全部内测 key（含使用状态与是否过期）。"""
        raw = await sp.global_get(KEY_KEYS, {})
        keys = raw if isinstance(raw, dict) else {}
        now = _now()
        out: dict[str, dict[str, Any]] = {}
        for key, item in keys.items():
            if not isinstance(item, dict):
                continue
            expires_at = item.get("expires_at")
            out[key] = {
                **item,
                "expired": bool(expires_at and float(expires_at) <= now),
                "used": bool(item.get("used_by")),
            }
        return out

    @staticmethod
    async def create_keys(
        count: int = 1,
        scope: str = ALL_PLUGINS,
        note: str = "",
        expires_in_days: float = 0,
    ) -> list[str]:
        """批量创建一次性 key。

        Args:
            count: 生成数量（1-50）。
            scope: 限定的插件注册名，或 ``*`` 表示全部内测插件。
            note: 备注，便于后台辨认发放对象。
            expires_in_days: 有效天数；``0`` 表示永久有效。

        Returns:
            新生成的 key 列表。
        """
        raw = await sp.global_get(KEY_KEYS, {})
        keys = raw if isinstance(raw, dict) else {}
        total = max(1, min(int(count or 1), 50))
        now = _now()
        expires_at = float(expires_in_days or 0)
        created: list[str] = []
        for _ in range(total):
            key = generate_key()
            while key in keys:
                key = generate_key()
            keys[key] = {
                "scope": str(scope or ALL_PLUGINS).strip() or ALL_PLUGINS,
                "note": str(note or "").strip(),
                "created_at": now,
                "expires_at": (now + expires_at * 86400) if expires_at > 0 else 0,
                "used_by": "",
                "used_at": 0,
            }
            created.append(key)
        await sp.global_put(KEY_KEYS, keys)
        return created

    @staticmethod
    async def delete_key(key: str) -> bool:
        """删除一个 key；同时回收它授予的访问权。"""
        target = normalize_key(key)
        raw = await sp.global_get(KEY_KEYS, {})
        keys = raw if isinstance(raw, dict) else {}
        item = keys.pop(target, None)
        if item is None:
            return False
        await sp.global_put(KEY_KEYS, keys)
        used_by = str(item.get("used_by") or "").strip()
        if used_by:
            await BetaGate.revoke(used_by, key=target)
        return True

    # ------------------------------------------------------------------ 授权

    @staticmethod
    async def list_grants() -> dict[str, dict[str, Any]]:
        """列出通过 key 兑换得到的授权（白名单用户不在其中）。"""
        raw = await sp.global_get(KEY_GRANTS, {})
        grants = raw if isinstance(raw, dict) else {}
        return {uid: item for uid, item in grants.items() if isinstance(item, dict)}

    @staticmethod
    async def grant(uid: str, plugins: list[str], source: str) -> dict[str, Any]:
        """给某个用户追加授权（合并已有授权，不覆盖）。

        Args:
            uid: 用户 ID。
            plugins: 插件注册名列表；含 ``*`` 时表示全部内测插件。
            source: 授权来源（如 key 值），仅用于后台展示。

        Returns:
            该用户最新的授权记录。
        """
        user = str(uid or "").strip()
        if not user:
            raise ValueError("uid 不能为空")
        raw = await sp.global_get(KEY_GRANTS, {})
        grants = raw if isinstance(raw, dict) else {}
        item = grants.get(user)
        item = item if isinstance(item, dict) else {}
        merged = list(dict.fromkeys(_as_str_list(item.get("plugins")) + list(plugins)))
        record = {
            "plugins": merged,
            "source": source,
            "at": _now(),
        }
        grants[user] = record
        await sp.global_put(KEY_GRANTS, grants)
        return record

    @staticmethod
    async def revoke(uid: str, key: str | None = None) -> bool:
        """撤销某个用户的授权。

        Args:
            uid: 用户 ID。
            key: 只撤销由该 key 授予的授权；``None`` 表示清空该用户全部授权。

        Returns:
            ``True`` 表示确有授权被移除。
        """
        user = str(uid or "").strip()
        raw = await sp.global_get(KEY_GRANTS, {})
        grants = raw if isinstance(raw, dict) else {}
        if user not in grants:
            return False
        if key is None:
            grants.pop(user, None)
        else:
            item = grants.get(user)
            item = item if isinstance(item, dict) else {}
            if str(item.get("source") or "") == normalize_key(key):
                grants.pop(user, None)
            else:
                return False
        await sp.global_put(KEY_GRANTS, grants)
        return True

    @staticmethod
    async def redeem(raw_key: str, uid: str) -> tuple[bool, str, list[str]]:
        """兑换一次性 key。

        Args:
            raw_key: 用户输入的内测码。
            uid: 兑换者用户 ID。

        Returns:
            ``(是否成功, 提示语, 授予的插件列表)``。
        """
        user = str(uid or "").strip()
        if not user:
            return False, "无法识别你的用户 ID，兑换失败。", []
        key = normalize_key(raw_key)
        if not key:
            return False, "请提供内测码：/内测 <内测码>", []

        raw = await sp.global_get(KEY_KEYS, {})
        keys = raw if isinstance(raw, dict) else {}
        item = keys.get(key)
        if not isinstance(item, dict):
            return False, "内测码无效，请核对后重试。", []

        used_by = str(item.get("used_by") or "").strip()
        if used_by and used_by != user:
            return False, "该内测码已被使用。", []
        expires_at = float(item.get("expires_at") or 0)
        if expires_at and expires_at <= _now():
            return False, "该内测码已过期。", []

        scope = str(item.get("scope") or ALL_PLUGINS).strip() or ALL_PLUGINS
        granted = [ALL_PLUGINS] if scope == ALL_PLUGINS else [scope]

        if not used_by:
            item["used_by"] = user
            item["used_at"] = _now()
            keys[key] = item
            await sp.global_put(KEY_KEYS, keys)

        await BetaGate.grant(user, granted, source=key)
        return True, "兑换成功。", granted

    # ------------------------------------------------------------------ 准入判定

    @staticmethod
    def _resolve_beta_plugins(raw_plugins: list[str]) -> set[str]:
        """把手写的内测插件名单解析成注册名集合。

        解析失败的名字按原样保留（插件可能尚未安装），插件装上后即可自动生效。
        """
        if not raw_plugins:
            return set()
        resolved, failed = names_of(raw_plugins)
        return set(resolved) | set(failed)

    @staticmethod
    async def build_context(event: Any) -> dict[str, Any] | None:
        """为当前消息构建一次判定上下文。

        Args:
            event: 当前消息事件。

        Returns:
            ``None`` 表示无需拦截（总开关关闭或没有配置内测插件），调用方应直接放行。
        """
        config = await BetaGate.get_config()
        if not config["enabled"]:
            return None
        beta_plugins = BetaGate._resolve_beta_plugins(config["plugins"])
        if not beta_plugins:
            return None

        uid = str(event.get_sender_id()) if event is not None else ""
        grants_raw = await sp.global_get(KEY_GRANTS, {})
        grants = grants_raw if isinstance(grants_raw, dict) else {}
        grant = grants.get(uid)
        grant_plugins = (
            set(_as_str_list((grant or {}).get("plugins"))) if grant else set()
        )

        is_admin = False
        if config["admin_bypass"]:
            try:
                from astrbot.builtin_stars.builtin_commands.tier_api import (
                    get_level_by_id,
                )

                is_admin = await get_level_by_id(uid) >= 1
            except Exception:
                is_admin = False

        return {
            "uid": uid,
            "beta_plugins": beta_plugins,
            "user_ids": set(config["user_ids"]),
            "grant_plugins": grant_plugins,
            "is_admin": is_admin,
            "block_message": config["block_message"],
        }

    @staticmethod
    def is_beta_plugin(plugin_name: str | None, context: dict[str, Any] | None) -> bool:
        """该插件是否受内测准入控制。"""
        if not plugin_name or not context:
            return False
        return plugin_name in context["beta_plugins"]

    @staticmethod
    def is_allowed(plugin_name: str | None, context: dict[str, Any] | None) -> bool:
        """判定当前用户能否使用该插件。

        Args:
            plugin_name: 插件注册名。
            context: :meth:`build_context` 的结果。

        Returns:
            ``True`` 表示放行；不受内测控制的插件恒为 ``True``。
        """
        if not BetaGate.is_beta_plugin(plugin_name, context):
            return True
        if context["uid"] and context["uid"] in context["user_ids"]:
            return True
        if context["is_admin"]:
            return True
        granted = context["grant_plugins"]
        if ALL_PLUGINS in granted:
            return True
        return bool(plugin_name and plugin_name in granted)

    @staticmethod
    def _is_command_handler(handler: Any) -> bool:
        """判断 handler 是否由用户显式命令触发（决定是否回提示语）。"""
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.filter.command_group import CommandGroupFilter

        for event_filter in getattr(handler, "event_filters", []) or []:
            if isinstance(event_filter, (CommandFilter, CommandGroupFilter)):
                return True
        return False

    @staticmethod
    async def filter_handlers(
        event: Any,
        handlers: list,
    ) -> tuple[list, list[str]]:
        """按内测准入过滤 handler 列表。

        命令类 handler 被拦截时会记录插件名，供调用方发送自定义提示；
        被动钩子被拦截时静默跳过。

        Args:
            event: 当前消息事件。
            handlers: 已通过其它过滤器的 handler 列表。

        Returns:
            ``(放行的 handler 列表, 被拦截的命令所属插件名列表)``。
        """
        context = await BetaGate.build_context(event)
        if not context:
            return handlers, []

        from astrbot.core.star.star import star_map

        allowed: list = []
        blocked_plugins: list[str] = []
        for handler in handlers:
            meta = star_map.get(handler.handler_module_path)
            plugin_name = getattr(meta, "name", None) if meta else None
            if getattr(meta, "reserved", False):
                allowed.append(handler)
                continue
            if BetaGate.is_allowed(plugin_name, context):
                allowed.append(handler)
                continue
            if BetaGate._is_command_handler(handler):
                logger.info(
                    "Beta gate blocked %s from user %s (plugin %s).",
                    handler.handler_name,
                    context["uid"],
                    plugin_name,
                )
                if plugin_name not in blocked_plugins:
                    blocked_plugins.append(plugin_name or "")
            else:
                logger.debug(
                    "Beta gate skipped passive handler %s (plugin %s).",
                    handler.handler_name,
                    plugin_name,
                )
        return allowed, blocked_plugins

    @staticmethod
    async def filter_toolset(toolset: Any, event: Any) -> int:
        """从工具集里移除当前用户无权使用的内测插件工具。

        Args:
            toolset: 待裁剪的工具集（就地修改）。
            event: 当前消息事件。

        Returns:
            被移除的工具数量。
        """
        if toolset is None or event is None:
            return 0
        context = await BetaGate.build_context(event)
        if not context:
            return 0

        from astrbot.core.star.star import star_map

        try:
            tools = list(toolset)
        except TypeError:
            return 0

        removed = 0
        for tool in tools:
            module_path = getattr(tool, "handler_module_path", None)
            meta = star_map.get(module_path) if module_path else None
            plugin_name = getattr(meta, "name", None) if meta else None
            if getattr(meta, "reserved", False):
                continue
            if BetaGate.is_allowed(plugin_name, context):
                continue
            try:
                toolset.remove_tool(tool.name)
                removed += 1
            except Exception:
                continue
        if removed:
            logger.debug(
                "Beta gate removed %s tool(s) for user %s.", removed, context["uid"]
            )
        return removed
