"""内置指令。

权限体系（三级超管 + 指令白名单 + 越级规则）随本内置插件提供，
配置项见同目录 ``_conf_schema.json``；分级同步到 sp 供其他模块查询，
见 ``tier_api``。

每条指令通过 ``self.perm.check(event, "<命令名>")`` 做权限判定；
操作其他用户的指令（/op /deop）额外做越级校验（``check_target``）。
"""

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.core.star.filter.command import GreedyStr

from .commands import (
    AdminCommands,
    ConversationCommands,
    HelpCommand,
    NameCommand,
    ProviderCommands,
    SetUnsetCommands,
    SIDCommand,
)
from .permission import (
    SETOP_LEVEL_KEY,
    SETOP_LEVEL_NAME,
    SETOP_LEVEL_RANK,
    TIER_RANK,
    PermissionManager,
)
from .tier_api import register_compat_modules, sync_tier_ids


class Main(star.Star):
    def __init__(self, context: star.Context, config=None) -> None:
        self.context = context
        # 走标准插件配置机制：data/config/builtin_commands_config.json
        # （内核在存在 _conf_schema.json 时以 config= 传入）
        if config is None:
            from pathlib import Path

            from astrbot.core.config import AstrBotConfig
            from astrbot.core.utils.astrbot_path import get_astrbot_config_path

            from .tier_api import load_builtin_schema

            # 必须传 schema，否则 AstrBotConfig 会回退到主配置 cmd_config.json
            config = AstrBotConfig(
                config_path=str(
                    Path(str(get_astrbot_config_path()))
                    / "builtin_commands_config.json"
                ),
                schema=load_builtin_schema(),
            )
        self._cfg = config
        self.config = dict(config) if not isinstance(config, dict) else dict(config)
        self.perm = PermissionManager(self.config)

        self.admin_c = AdminCommands(self.context)
        self.conversation_c = ConversationCommands(self.context)
        self.help_c = HelpCommand(self.context)
        self.name_c = NameCommand(self.context)
        self.provider_c = ProviderCommands(self.context)
        self.setunset_c = SetUnsetCommands(self.context)
        self.sid_c = SIDCommand(self.context)

    async def initialize(self) -> None:
        """把三级超管分级同步到 sp 全局存储，供其他模块查询。"""
        # 旧插件 builtin_commands_extension 已并入内核，登记兼容别名
        register_compat_modules()
        await sync_tier_ids(self.config)

    # ==================== 基础指令 ====================

    @filter.command("help")
    async def help(self, event: AstrMessageEvent) -> None:
        """查看帮助与指令列表"""
        if not await self.perm.check(event, "help"):
            return
        await self.help_c.help(event)

    @filter.command("sid")
    async def sid(self, event: AstrMessageEvent) -> None:
        """获取会话 ID 及相关信息"""
        if not await self.perm.check(event, "sid"):
            return
        await self.sid_c.sid(event)

    @filter.command("name")
    async def name(self, event: AstrMessageEvent, alias: GreedyStr) -> None:
        """设置当前会话的展示别名"""
        if not await self.perm.check(event, "name"):
            return
        await self.name_c.name(event, alias)

    @filter.command("reset")
    async def reset(self, message: AstrMessageEvent) -> None:
        """开启新对话，保留历史"""
        if not await self.perm.check(message, "reset"):
            return
        await self.conversation_c.new_conv(message)

    @filter.command("stop")
    async def stop(self, message: AstrMessageEvent) -> None:
        """停止当前会话正在运行的 Agent 任务"""
        if not await self.perm.check(message, "stop"):
            return
        await self.conversation_c.stop(message)

    @filter.command("new")
    async def new_conv(self, message: AstrMessageEvent) -> None:
        """开启新对话，保留历史"""
        if not await self.perm.check(message, "new"):
            return
        await self.conversation_c.new_conv(message)

    @filter.command("stats")
    async def stats(self, message: AstrMessageEvent) -> None:
        """查看当前对话的 Token 用量统计"""
        if not await self.perm.check(message, "stats"):
            return
        await self.conversation_c.stats(message)

    @filter.command("dashboard_update")
    async def update_dashboard(self, event: AstrMessageEvent) -> None:
        """更新 AstrBot WebUI 管理面板"""
        if not await self.perm.check(event, "dashboard_update"):
            return
        await self.admin_c.update_dashboard(event)

    @filter.command("set")
    async def set_variable(self, event: AstrMessageEvent, key: str, value: str) -> None:
        """设置会话变量"""
        if not await self.perm.check(event, "set"):
            return
        await self.setunset_c.set_variable(event, key, value)

    @filter.command("unset")
    async def unset_variable(self, event: AstrMessageEvent, key: str) -> None:
        """移除会话变量"""
        if not await self.perm.check(event, "unset"):
            return
        await self.setunset_c.unset_variable(event, key)

    # ==================== Provider / 模型 ====================

    @filter.command("provider")
    async def provider(
        self,
        event: AstrMessageEvent,
        idx: str | int | None = None,
        idx2: int | None = None,
    ) -> None:
        """查看或者切换 LLM Provider"""
        if not await self.perm.check(event, "provider"):
            return
        await self.provider_c.provider(event, idx, idx2)

    # ==================== 管理员授权（三级超管）====================

    @staticmethod
    def _split_ids(raw) -> list[str]:
        text = str(raw or "").replace("，", ",").replace("；", ";")
        return [s.strip() for s in text.split(",") if s.strip()]

    @staticmethod
    def _join_ids(items) -> str:
        return ",".join(dict.fromkeys(s for s in items if s))

    async def _write_tier(self, key: str, target: str, action: str) -> bool:
        """把某个 ID 增删进某级超管列表，落盘后同步到 sp。"""
        items = self._split_ids(self.config.get(key))
        if action == "add":
            if target not in items:
                items.append(target)
        else:
            items = [s for s in items if s != target]
        self.config[key] = self._join_ids(items)
        saved = self._save_config()
        await sync_tier_ids(self.config)
        return saved

    def _save_config(self) -> bool:
        """把改动写回插件配置文件；失败时返回 False（内存态仍已生效）。"""
        cfg = self._cfg
        if cfg is None or isinstance(cfg, dict):
            return False
        try:
            for key, value in self.config.items():
                cfg[key] = value
            cfg.save_config()
            return True
        except Exception:
            return False

    async def _remove_from_all_tiers(self, target: str) -> None:
        """把一个 ID 从所有超管层级中移除（避免一人多级）。"""
        for key in SETOP_LEVEL_KEY.values():
            items = [s for s in self._split_ids(self.config.get(key)) if s != target]
            self.config[key] = self._join_ids(items)

    @filter.command("op")
    async def op(self, event: AstrMessageEvent, admin_id: str = "") -> None:
        """授权管理员（默认普通超管）。op <admin_id>（中级超管及以上可用）"""
        target = str(admin_id or event.get_sender_id()).strip()
        if not await self.perm.check_target(
            event,
            "op",
            target,
            granted_rank=SETOP_LEVEL_RANK[1],
            min_rank=TIER_RANK["mid_admin"],
        ):
            return
        await self._remove_from_all_tiers(target)
        saved = await self._write_tier(SETOP_LEVEL_KEY[1], target, "add")
        suffix = "" if saved else "\n（注意：配置未能落盘，重启后可能失效）"
        event.set_result(
            MessageEventResult().message(
                f"已将 {target} 设为{SETOP_LEVEL_NAME[1]}。\n"
                f"如需调整等级：/setop {target} <0|1|2|3>{suffix}",
            ),
        )

    @filter.command("setop")
    async def setop(
        self,
        event: AstrMessageEvent,
        admin_id: str = "",
        level: int | None = None,
    ) -> None:
        """设置管理员等级。setop <admin_id> <0|1|2|3>（仅最高超管可用）"""
        role = await self.perm.resolve_role(event)
        # 硬限制：只有最高超管能用 /setop
        if role != "top_admin":
            event.set_result(
                MessageEventResult().message("只有最高超管能使用 /setop 指令。"),
            )
            return
        if level not in SETOP_LEVEL_RANK:
            event.set_result(
                MessageEventResult().message(
                    "使用方法: /setop <id> <等级>\n"
                    "0=普通成员，1=普通超管，2=中级超管，3=最高超管。",
                ),
            )
            return
        # 防提权：不能授予高于自身的等级
        if TIER_RANK.get(role, 0) < SETOP_LEVEL_RANK[level]:
            event.set_result(
                MessageEventResult().message(
                    "您无权授予该层级：不能授予高于您自身的权限等级。",
                ),
            )
            return
        target = str(admin_id).strip()
        await self._remove_from_all_tiers(target)
        if level in SETOP_LEVEL_KEY:
            saved = await self._write_tier(SETOP_LEVEL_KEY[level], target, "add")
            note = "" if saved else "\n（注意：配置未能落盘，重启后可能失效）"
            event.set_result(
                MessageEventResult().message(
                    f"已将 {target} 的等级设置为 {SETOP_LEVEL_NAME[level]}。{note}",
                ),
            )
        else:
            saved = self._save_config()
            await sync_tier_ids(self.config)
            note = "" if saved else "\n（注意：配置未能落盘，重启后可能失效）"
            event.set_result(
                MessageEventResult().message(f"已将 {target} 降为普通成员。{note}"),
            )

    @filter.command("deop")
    async def deop(self, event: AstrMessageEvent, admin_id: str = "") -> None:
        """取消管理员授权。deop <admin_id>"""
        target = str(admin_id or event.get_sender_id()).strip()
        if not await self.perm.check_target(
            event, "deop", target, granted_rank=SETOP_LEVEL_RANK[0]
        ):
            return
        await self._remove_from_all_tiers(target)
        saved = self._save_config()
        await sync_tier_ids(self.config)
        note = "" if saved else "\n（注意：配置未能落盘，重启后可能失效）"
        event.set_result(MessageEventResult().message(f"已取消 {target} 的管理员授权。{note}"))
