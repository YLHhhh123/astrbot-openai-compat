"""内置指令的权限管理器（三级超管 + 越级规则 + 指令白名单）。

本模块原为独立插件 ``builtin_commands_extension``，现已并入内核内置插件
``builtin_commands``，不再需要单独安装。

层级（从高到低，越级规则据此判定）：
    top_admin（最高超管，5） > mid_admin（中级超管，4） > normal_admin（普通超管，3）
    > group_owner（群主，2） > group_admin（群管理，1） > member（普通成员，0）

三级超管身份完全由配置的三份 ID 列表决定（``top_admin_ids`` / ``mid_admin_ids`` /
``normal_admin_ids``），取代 AstrBot 原��的单一 ``admins_id`` 分级。

每级可自定义：
    1. 指令白名单 ``<tier>_commands``：留空=全部；"禁用"/"disabled"=全部禁止；
       其他=逗号分隔名单（支持 "plugin ls" 这类含空格的子命令）
    2. 禁止插件名单 ``<tier>_forbidden_plugins``：命中即禁止该插件全部指令。
       同时兼容历史插件名 ``builtin_commands_extension``（旧插件已并入内核）。

越级规则：对会操作其他用户的指令（/op /setop /deop），发起者层级必须 >= 目标层级。
"""

import re

from astrbot.api.event import AstrMessageEvent, MessageEventResult

from .tier_api import extract_uid

# 本内置插件注册名与 plugin_id（author/name）
PLUGIN_NAME = "builtin_commands"
PLUGIN_ID = "astrbot/builtin_commands"

# 历史名称：旧独立插件并入内核后，仍允许按旧名禁用（配置兼容）
LEGACY_PLUGIN_NAMES = {"builtin_commands_extension", "astrbot/builtin_commands_extension"}

# 层级序号，用于越级比较
TIER_RANK = {
    "top_admin": 5,
    "mid_admin": 4,
    "normal_admin": 3,
    "group_owner": 2,
    "group_admin": 1,
    "member": 0,
}

# /setop 等级 -> 对应层级 rank（用于防提权校验）
# 0=普通成员(member)，1=普通超管(normal_admin)，2=中级超管(mid_admin)，3=最高超管(top_admin)
SETOP_LEVEL_RANK = {0: 0, 1: 3, 2: 4, 3: 5}
SETOP_LEVEL_NAME = {0: "普通成员", 1: "普通超管", 2: "中级超管", 3: "最高超管"}
SETOP_LEVEL_KEY = {1: "normal_admin_ids", 2: "mid_admin_ids", 3: "top_admin_ids"}

# 角色 -> (指令白名单配置键, 禁止插件配置键)
_ROLE_KEYS = {
    "top_admin": ("top_admin_commands", "top_admin_forbidden_plugins"),
    "mid_admin": ("mid_admin_commands", "mid_admin_forbidden_plugins"),
    "normal_admin": ("normal_admin_commands", "normal_admin_forbidden_plugins"),
    "group_owner": ("group_owner_commands", "group_owner_forbidden_plugins"),
    "group_admin": ("group_admin_commands", "group_admin_forbidden_plugins"),
    "member": ("member_commands", "member_forbidden_plugins"),
}

# 视为"禁用"的关键字
_DISABLED_KEYWORDS = {"禁用", "禁用全部", "disabled", "off", "none", "deny", "false", "0"}

# 高危指令硬限制：非超管角色（普通成员/群主/群管理）一律拒绝，白名单配置无法覆盖。
# - plugin get：从网络安装任意插件 = 任意代码执行
# - plugin off/on：禁用/启用任意插件（可把安全插件关掉造成 DoS）
_HIGH_RISK_COMMANDS = {"plugin get", "plugin off", "plugin on", "dashboard_update"}


class PermissionManager:
    def __init__(self, config: dict | None = None) -> None:
        self._config = config if isinstance(config, dict) else {}

    # ------------------------------------------------------------------ 配置读取

    def _get(self, key: str) -> str:
        return str(self._config.get(key) or "").strip()

    def _id_set(self, key: str) -> set[str]:
        raw = self._get(key)
        if not raw:
            return set()
        return {s for s in re.split(r"[,，;；\s]+", raw) if s}

    def _plugin_set(self, key: str) -> set[str]:
        raw = self._get(key)
        if not raw:
            return set()
        return {s for s in re.split(r"[,，;；\s]+", raw) if s}

    # ------------------------------------------------------------------ 指令范围

    @staticmethod
    def _parse_scope(raw: str):
        """把指令白名单配置解析为 'all' | 'disabled' | 指令名集合。"""
        raw = (raw or "").strip()
        if raw == "":
            return "all"
        if raw.lower() in _DISABLED_KEYWORDS:
            return "disabled"
        # 仅按逗号/中文逗号/分号分隔（指令名本身可含空格，如 "plugin ls"）
        names = re.split(r"[,，;；]+", raw)
        return {n.strip() for n in names if n.strip()}

    def _command_in_scope(self, command: str, scope) -> bool:
        if scope == "all":
            return True
        if scope == "disabled":
            return False
        if command in scope:
            return True
        # 指令组回退：如 "plugin ls" 命中顶层 "plugin"
        if " " in command and command.split(" ", 1)[0] in scope:
            return True
        return False

    # ------------------------------------------------------------------ 角色解析

    async def resolve_role(self, event: AstrMessageEvent) -> str:
        """解析发送者在当前消息中的最高角色。"""
        sender = str(event.get_sender_id())
        suid = extract_uid(sender)

        def _hit(key: str) -> bool:
            return any(extract_uid(e) == suid for e in self._id_set(key))

        if _hit("top_admin_ids"):
            return "top_admin"
        if _hit("mid_admin_ids"):
            return "mid_admin"
        if _hit("normal_admin_ids"):
            return "normal_admin"

        group_id = event.get_group_id()
        if group_id:
            try:
                group = await event.get_group()
            except Exception:
                group = None
            if group:
                if group.group_owner and sender == str(group.group_owner):
                    return "group_owner"
                if group.group_admins and sender in [
                    str(a) for a in group.group_admins
                ]:
                    return "group_admin"
        return "member"

    def resolve_tier_by_id(self, target_id: str) -> str:
        """解析某个用户 ID 的层级（用于越级校验的目标方）。"""
        tid = extract_uid(target_id or "")
        if not tid:
            return "member"

        def _hit(key: str) -> bool:
            return any(extract_uid(e) == tid for e in self._id_set(key))

        if _hit("top_admin_ids"):
            return "top_admin"
        if _hit("mid_admin_ids"):
            return "mid_admin"
        if _hit("normal_admin_ids"):
            return "normal_admin"
        return "member"

    # ------------------------------------------------------------------ 权限判定

    def is_allowed(self, role: str, command: str) -> bool:
        """判断某角色是否允许使用某指令（含禁止插件检查）。"""
        keys = _ROLE_KEYS.get(role)
        if keys is None:
            return False
        cmd_key, forbidden_key = keys

        # 高危指令硬限制：非超管角色一律拒绝（优先于白名单判断）
        if role not in ("top_admin", "mid_admin", "normal_admin"):
            if command in _HIGH_RISK_COMMANDS:
                return False

        scope = self._parse_scope(self._get(cmd_key))
        if not self._command_in_scope(command, scope):
            return False

        forbidden = self._plugin_set(forbidden_key)
        if forbidden and (
            forbidden & (LEGACY_PLUGIN_NAMES | {PLUGIN_NAME, PLUGIN_ID})
        ):
            return False
        return True

    async def check(self, event: AstrMessageEvent, command: str) -> bool:
        """权限检查入口：允许返回 True，否则设置"无权限"提示并返回 False。"""
        role = await self.resolve_role(event)
        if self.is_allowed(role, command):
            return True
        self._deny(event, command)
        return False

    async def check_target(
        self,
        event: AstrMessageEvent,
        command: str,
        target_id: str,
        granted_rank: int | None = None,
        min_rank: int | None = None,
    ) -> bool:
        """带越级校验的权限检查，用于会操作其他用户的指令（/op /setop /deop）。

        granted_rank: 该操作将要授予的目标层级 rank（/setop 用），用于"防提权"：
        发起者 rank 必须 >= 授予 rank，即不能授予高于自身等级的权限。
        min_rank: 使用该指令所需的最低发起者 rank（例如 /op 要求中级超管及以上）。
        """
        role = await self.resolve_role(event)
        if not self.is_allowed(role, command):
            self._deny(event, command)
            return False

        issuer_rank = TIER_RANK.get(role, 0)
        if min_rank is not None and issuer_rank < min_rank:
            event.set_result(
                MessageEventResult().message(
                    f"您 (ID {event.get_sender_id()}) 的层级不足以使用 /{command} 指令。",
                ),
            )
            return False

        tid = str(target_id or "").strip()
        if tid:
            target_role = self.resolve_tier_by_id(tid)
            if issuer_rank < TIER_RANK.get(target_role, 0):
                event.set_result(
                    MessageEventResult().message(
                        f"您 (ID {event.get_sender_id()}) 无权对该用户执行操作："
                        "目标用户的权限层级高于您。",
                    ),
                )
                return False
            if granted_rank is not None and issuer_rank < granted_rank:
                event.set_result(
                    MessageEventResult().message(
                        f"您 (ID {event.get_sender_id()}) 无权授予该层级："
                        "不能授予高于您自身的权限等级。",
                    ),
                )
                return False
        return True

    def _deny(self, event: AstrMessageEvent, command: str) -> None:
        event.set_result(
            MessageEventResult().message(
                f"您 (ID {event.get_sender_id()}) 没有权限使用 /{command} 指令。",
            ),
        )
