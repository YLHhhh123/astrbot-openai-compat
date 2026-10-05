"""会话插件管理器 - 负责管理每个会话的插件启停状态"""

from astrbot.core import logger, sp
from astrbot.core.platform.astr_message_event import AstrMessageEvent


class SessionPluginManager:
    """管理会话级别的插件启停状态"""

    @staticmethod
    async def is_plugin_enabled_for_session(
        session_id: str,
        plugin_name: str,
    ) -> bool:
        """检查插件是否在指定会话中启用

        Args:
            session_id: 会话ID (unified_msg_origin)
            plugin_name: 插件名称

        Returns:
            bool: True表示启用，False表示禁用

        """
        # 获取会话插件配置
        session_plugin_config = await sp.get_async(
            scope="umo",
            scope_id=session_id,
            key="session_plugin_config",
            default={},
        )
        session_config = session_plugin_config.get(session_id, {})

        enabled_plugins = session_config.get("enabled_plugins", [])
        disabled_plugins = session_config.get("disabled_plugins", [])

        # 如果插件在禁用列表中，返回False
        if plugin_name in disabled_plugins:
            return False

        # 如果插件在启用列表中，返回True
        if plugin_name in enabled_plugins:
            return True

        # 如果都没有配置，默认为启用（兼容性考虑）
        return True

    @staticmethod
    async def filter_handlers_by_session(
        event: AstrMessageEvent,
        handlers: list,
    ) -> list:
        """根据会话配置过滤处理器列表

        Args:
            event: 消息事件
            handlers: 原始处理器列表

        Returns:
            List: 过滤后的处理器列表

        """
        # 延迟导入：astrbot.core.star 包内部存在相互引用，顶层导入会形成环
        from astrbot.core.star.plugin_category_manager import (
            PluginCategoryManager,
        )
        from astrbot.core.star.star import star_map

        session_id = event.unified_msg_origin
        filtered_handlers = []

        session_plugin_config = await sp.get_async(
            scope="umo",
            scope_id=session_id,
            key="session_plugin_config",
            default={},
        )
        session_config = session_plugin_config.get(session_id, {})
        disabled_plugins = session_config.get("disabled_plugins", [])

        # 入口（平台 / 特殊入口）+ 插件类别策略，一次取齐供本轮复用
        category_context = await PluginCategoryManager.build_context()
        entry = PluginCategoryManager.entry_of(event)
        extra_plugins, extra_categories = (
            PluginCategoryManager.extra_allowed_from_event(event)
        )

        for handler in handlers:
            # 获取处理器对应的插件
            plugin = star_map.get(handler.handler_module_path)
            if not plugin:
                # 如果找不到插件元数据，允许执行（可能是系统插件）
                filtered_handlers.append(handler)
                continue

            # 跳过保留插件（系统插件）
            if plugin.reserved:
                filtered_handlers.append(handler)
                continue

            if plugin.name is None:
                continue

            # 检查插件是否在当前会话中启用
            if plugin.name in disabled_plugins:
                logger.debug(
                    f"Plugin {plugin.name} is disabled in session {session_id}; "
                    f"skipping handler {handler.handler_name}.",
                )
                continue

            # 入口类别过滤：功能性 / 互动性
            declared = getattr(plugin, "category", None)
            if not PluginCategoryManager.is_allowed(
                entry=entry,
                plugin_name=plugin.name,
                declared=declared,
                context=category_context,
                extra_plugins=extra_plugins,
                extra_categories=extra_categories,
            ):
                logger.debug(
                    f"Plugin {plugin.name} [{declared or 'default'}] is not allowed "
                    f"at entry '{entry}'; skipping handler {handler.handler_name}.",
                )
                continue

            filtered_handlers.append(handler)

        return filtered_handlers
