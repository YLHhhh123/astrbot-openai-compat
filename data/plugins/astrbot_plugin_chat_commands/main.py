from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import register
from astrbot.core.star.filter.command import GreedyStr

from .commands import (
    ConversationCommands,
    LLMCommands,
    PersonaCommands,
    PluginCommands,
    ProviderCommands,
)
from astrbot.builtin_stars.builtin_commands.permission import PermissionManager
from astrbot.builtin_stars.builtin_commands.tier_api import load_tier_config


@register(
    "astrbot_plugin_chat_commands",
    "WorkBuddy",
    "会话指令集：插件管理、Provider/模型管理、人格与会话管理等扩展指令。权限分级由内置插件 builtin_commands 统一提供。",
    "1.0.0",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)
        self.context = context
        # 权限配置与分级判定统一由内核内置插件 builtin_commands 提供
        self.config = load_tier_config(context)
        self.perm = PermissionManager(self.config)

        self.llm_c = LLMCommands(self.context)
        self.persona_c = PersonaCommands(self.context)
        self.plugin_c = PluginCommands(self.context)
        self.provider_c = ProviderCommands(self.context)

    async def initialize(self) -> None:
        """指令集本身不持有权限配置，仅确认内核分级已就绪。"""
        self.config = load_tier_config(self.context)
        self.perm = PermissionManager(self.config)

    # ==================== 核心内置指令 ====================











    @filter.command("llm")
    async def llm(self, event: AstrMessageEvent) -> None:
        """开启/关闭 LLM"""
        if not await self.perm.check(event, "llm"):
            return
        await self.llm_c.llm(event)

    @filter.command_group("plugin")
    def plugin(self) -> None:
        """插件管理"""

    @plugin.command("ls")
    async def plugin_ls(self, event: AstrMessageEvent) -> None:
        """获取已经安装的插件列表"""
        if not await self.perm.check(event, "plugin ls"):
            return
        await self.plugin_c.plugin_ls(event)

    @plugin.command("off")
    async def plugin_off(self, event: AstrMessageEvent, plugin_name: str = "") -> None:
        """禁用插件"""
        if not await self.perm.check(event, "plugin off"):
            return
        await self.plugin_c.plugin_off(event, plugin_name)

    @plugin.command("on")
    async def plugin_on(self, event: AstrMessageEvent, plugin_name: str = "") -> None:
        """启用插件"""
        if not await self.perm.check(event, "plugin on"):
            return
        await self.plugin_c.plugin_on(event, plugin_name)

    @plugin.command("get")
    async def plugin_get(self, event: AstrMessageEvent, plugin_repo: str = "") -> None:
        """安装插件"""
        if not await self.perm.check(event, "plugin get"):
            return
        await self.plugin_c.plugin_get(event, plugin_repo)

    @plugin.command("help")
    async def plugin_help(self, event: AstrMessageEvent, plugin_name: str = "") -> None:
        """获取插件帮助"""
        if not await self.perm.check(event, "plugin help"):
            return
        await self.plugin_c.plugin_help(event, plugin_name)





    @filter.command("model")
    async def model_ls(
        self,
        message: AstrMessageEvent,
        idx_or_name: int | str | None = None,
    ) -> None:
        """查看或者切换模型"""
        if not await self.perm.check(message, "model"):
            return
        await self.provider_c.model_ls(message, idx_or_name)

    @filter.command("history")
    async def his(self, message: AstrMessageEvent, page: int = 1) -> None:
        """查看对话记录"""
        if not await self.perm.check(message, "history"):
            return
        await self.conversation_c.his(message, page)

    @filter.command("ls")
    async def convs(self, message: AstrMessageEvent, page: int = 1) -> None:
        """查看对话列表"""
        if not await self.perm.check(message, "ls"):
            return
        await self.conversation_c.convs(message, page)

    @filter.command("groupnew")
    async def groupnew_conv(self, message: AstrMessageEvent, sid: str) -> None:
        """创建新群聊对话"""
        if not await self.perm.check(message, "groupnew"):
            return
        await self.conversation_c.groupnew_conv(message, sid)

    @filter.command("switch")
    async def switch_conv(
        self, message: AstrMessageEvent, index: int | None = None
    ) -> None:
        """通过 /ls 前面的序号切换对话"""
        if not await self.perm.check(message, "switch"):
            return
        await self.conversation_c.switch_conv(message, index)

    @filter.command("rename")
    async def rename_conv(self, message: AstrMessageEvent, new_name: str) -> None:
        """重命名对话"""
        if not await self.perm.check(message, "rename"):
            return
        await self.conversation_c.rename_conv(message, new_name)

    @filter.command("del")
    async def del_conv(self, message: AstrMessageEvent) -> None:
        """删除当前对话"""
        if not await self.perm.check(message, "del"):
            return
        await self.conversation_c.del_conv(message)

    @filter.command("persona")
    async def persona(self, message: AstrMessageEvent) -> None:
        """查看或者切换 Persona"""
        if not await self.perm.check(message, "persona"):
            return
        await self.persona_c.persona(message)
