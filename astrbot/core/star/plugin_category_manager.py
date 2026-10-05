"""插件类别与入口策略管理器。

AstrBot 的插件按能力性质分两类：

- ``functional``（功能性）：通过 LLM 工具对外提供能力（如家居控制、天气查询），
  不依赖平台社交能力，因此在**所有入口**都可用。
- ``interactive``（互动性）：依赖平台社交能力（收发消息、@、点赞、群管理、
  主动发言等），在能力受限的入口（个人微信 openclaw、OpenAI 兼容 HTTP 接口）
  不可用。

类别来源优先级：**内核配置覆盖** > **插件元数据声明** > **默认 interactive**。
默认 interactive 是保守选择：把互动性插件误判为功能性，会在受限入口触发
平台不支持的调用而报错；反向误判只是少用几个工具。

入口（entry）标识取事件的 ``_entry`` 扩展字段（供 HTTP 等特殊入口自报身份），
未设置时回落到平台适配器名 ``event.get_platform_name()``
（如 ``aiocqhttp`` / ``weixin_oc`` / ``webchat``）。

存储位置：共享偏好（``sp``）的全局作用域，键名见 ``KEY_*`` 常量。
"""

from __future__ import annotations

from typing import Any

from astrbot.core import logger, sp

CATEGORY_FUNCTIONAL = "functional"
CATEGORY_INTERACTIVE = "interactive"
VALID_CATEGORIES: tuple[str, ...] = (CATEGORY_FUNCTIONAL, CATEGORY_INTERACTIVE)
DEFAULT_CATEGORY = CATEGORY_INTERACTIVE

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_FUNCTIONAL: "功能性",
    CATEGORY_INTERACTIVE: "互动性",
}

# 事件的入口标识扩展字段（由 openai_compat 等特殊入口写入）
ENTRY_EXTRA_KEY = "_entry"
# HTTP 入口按 Key 额外放开的插件列表（由 openai_compat 写入）
EXTRA_ALLOWED_PLUGINS_EXTRA_KEY = "_extra_allowed_plugins"
# HTTP 入口按 Key 额外放开的类别（通常为空；留作扩展）
EXTRA_ALLOWED_CATEGORIES_EXTRA_KEY = "_extra_allowed_categories"

KEY_OVERRIDES = "plugin_category_overrides"
KEY_ENTRY_POLICY = "entry_category_policy"

ALL_CATEGORIES: list[str] = [CATEGORY_FUNCTIONAL, CATEGORY_INTERACTIVE]

# 内置默认入口策略。未列出的入口取 "default"，保证既有行为不变。
DEFAULT_ENTRY_POLICY: dict[str, list[str]] = {
    "default": list(ALL_CATEGORIES),
    # 个人微信（腾讯 openclaw-weixin + ClawBot）：互动类插件依赖的社交能力不可用
    "weixin_oc": [CATEGORY_FUNCTIONAL],
    # OpenAI 兼容 HTTP 接口：默认只放开功能性，可按 Key 额外放开（见 EXTRA_ALLOWED_*）
    "openai_compat": [CATEGORY_FUNCTIONAL],
}


class PluginCategoryManager:
    """插件类别与入口策略的读写与判定。"""

    # ------------------------------------------------------------------
    # 类别判定
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_category(value: Any) -> str:
        """把任意输入规整为合法类别；非法值回落默认 interactive。"""
        text = str(value).strip().lower() if value is not None else ""
        if text in VALID_CATEGORIES:
            return text
        if text in ("func", "function", "tool", "工具", "功能性"):
            return CATEGORY_FUNCTIONAL
        if text in ("inter", "interact", "chat", "互动", "互动性"):
            return CATEGORY_INTERACTIVE
        return DEFAULT_CATEGORY

    @staticmethod
    async def get_category_overrides() -> dict[str, str]:
        """内核侧类别覆盖表：``{插件名: 类别}``。"""
        raw = await sp.global_get(KEY_OVERRIDES, {})
        if not isinstance(raw, dict):
            return {}
        return {
            str(name): PluginCategoryManager.normalize_category(value)
            for name, value in raw.items()
            if str(name).strip()
        }

    @staticmethod
    async def get_classified_plugins() -> set[str]:
        """已被显式设置过类别的插件名集合。"""
        return set((await PluginCategoryManager.get_category_overrides()).keys())

    @staticmethod
    async def set_category(plugin_name: str, category: str) -> str:
        """设置插件类别（写入覆盖表）。"""
        name = str(plugin_name).strip()
        if not name:
            raise ValueError("plugin_name is required")
        normalized = PluginCategoryManager.normalize_category(category)
        overrides = await PluginCategoryManager.get_category_overrides()
        overrides[name] = normalized
        await sp.global_put(KEY_OVERRIDES, overrides)
        logger.debug("插件 %s 类别设为 %s", name, normalized)
        return normalized

    @staticmethod
    async def clear_category(plugin_name: str) -> bool:
        """移除覆盖，回落到元数据声明 / 默认值。"""
        name = str(plugin_name).strip()
        overrides = await PluginCategoryManager.get_category_overrides()
        if name not in overrides:
            return False
        overrides.pop(name, None)
        await sp.global_put(KEY_OVERRIDES, overrides)
        return True

    @staticmethod
    def resolve_category(plugin_name: str | None, declared: str | None, overrides: dict[str, str]) -> str:
        """按优先级解析插件类别：覆盖表 > 元数据声明 > 默认。"""
        if plugin_name and plugin_name in overrides:
            return overrides[plugin_name]
        if declared:
            return PluginCategoryManager.normalize_category(declared)
        return DEFAULT_CATEGORY

    @staticmethod
    def category_source(plugin_name: str | None, declared: str | None, overrides: dict[str, str]) -> str:
        """类别来源：``override`` / ``metadata`` / ``default``。"""
        if plugin_name and plugin_name in overrides:
            return "override"
        if declared:
            return "metadata"
        return "default"

    # ------------------------------------------------------------------
    # 入口策略
    # ------------------------------------------------------------------

    @staticmethod
    async def get_entry_policy() -> dict[str, list[str]]:
        """读取入口策略，与内置默认合并（用户配置优先）。"""
        policy: dict[str, list[str]] = {
            entry: list(values) for entry, values in DEFAULT_ENTRY_POLICY.items()
        }
        raw = await sp.global_get(KEY_ENTRY_POLICY, {})
        if isinstance(raw, dict):
            for entry, values in raw.items():
                key = str(entry).strip()
                if not key:
                    continue
                if isinstance(values, list):
                    cleaned = [
                        PluginCategoryManager.normalize_category(item)
                        for item in values
                    ]
                    # 去重保序
                    policy[key] = list(dict.fromkeys(cleaned))
        return policy

    @staticmethod
    async def set_entry_policy(policy: dict[str, list[str]]) -> dict[str, list[str]]:
        """整体写入入口策略（只保存与内置默认不同的项，避免固化默认值）。"""
        if not isinstance(policy, dict):
            raise ValueError("policy must be a dict of entry -> categories")
        stored: dict[str, list[str]] = {}
        for entry, values in policy.items():
            key = str(entry).strip()
            if not key:
                continue
            if not isinstance(values, list):
                continue
            cleaned = [
                PluginCategoryManager.normalize_category(item) for item in values
            ]
            cleaned = list(dict.fromkeys(cleaned))
            if key in DEFAULT_ENTRY_POLICY and cleaned == DEFAULT_ENTRY_POLICY[key]:
                continue
            stored[key] = cleaned
        await sp.global_put(KEY_ENTRY_POLICY, stored)
        return await PluginCategoryManager.get_entry_policy()

    @staticmethod
    def entry_of(event: Any) -> str:
        """解析事件所属入口标识。"""
        try:
            declared = event.get_extra(ENTRY_EXTRA_KEY)
        except Exception:
            declared = None
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
        try:
            return str(event.get_platform_name() or "").strip() or "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    def allowed_categories(entry: str, policy: dict[str, list[str]]) -> set[str]:
        """该入口允许的类别集合。"""
        values = policy.get(entry)
        if values is None:
            values = policy.get("default", list(ALL_CATEGORIES))
        return set(values)

    # ------------------------------------------------------------------
    # 综合判定
    # ------------------------------------------------------------------

    @staticmethod
    async def build_context() -> dict[str, Any]:
        """一次性取齐判定所需的配置，供每轮消息过滤复用。"""
        return {
            "overrides": await PluginCategoryManager.get_category_overrides(),
            "policy": await PluginCategoryManager.get_entry_policy(),
        }

    @staticmethod
    def is_allowed(
        *,
        entry: str,
        plugin_name: str | None,
        declared: str | None,
        context: dict[str, Any],
        extra_plugins: set[str] | None = None,
        extra_categories: set[str] | None = None,
    ) -> bool:
        """判定插件在给定入口下是否可用。

        Args:
            entry: 入口标识。
            plugin_name: 插件名（``None`` 时视为系统插件，放行）。
            declared: 插件元数据声明的类别。
            context: :meth:`build_context` 的结果。
            extra_plugins: 额外放行的插件名（如 HTTP 入口按 Key 放开）。
            extra_categories: 额外放行的类别。

        Returns:
            ``True`` 表示允许在该入口运行。
        """
        if not plugin_name:
            return True
        if extra_plugins and plugin_name in extra_plugins:
            return True
        category = PluginCategoryManager.resolve_category(
            plugin_name, declared, context.get("overrides") or {}
        )
        if extra_categories and category in extra_categories:
            return True
        allowed = PluginCategoryManager.allowed_categories(
            entry, context.get("policy") or {}
        )
        return category in allowed

    @staticmethod
    def plugin_of_handler(handler: Any) -> Any:
        """取 handler 对应的插件元数据（找不到返回 ``None``）。"""
        from astrbot.core.star.star import star_map

        module_path = getattr(handler, "handler_module_path", None)
        if not module_path:
            return None
        return star_map.get(module_path)

    @staticmethod
    def extra_allowed_from_event(event: Any) -> tuple[set[str], set[str]]:
        """读取事件上按入口额外放行的插件 / 类别（由 http 入口写入）。"""
        plugins: set[str] = set()
        categories: set[str] = set()
        try:
            raw_plugins = event.get_extra(EXTRA_ALLOWED_PLUGINS_EXTRA_KEY)
            if isinstance(raw_plugins, (list, tuple, set)):
                plugins = {str(item) for item in raw_plugins if str(item).strip()}
            raw_categories = event.get_extra(EXTRA_ALLOWED_CATEGORIES_EXTRA_KEY)
            if isinstance(raw_categories, (list, tuple, set)):
                categories = {
                    PluginCategoryManager.normalize_category(item)
                    for item in raw_categories
                    if str(item).strip()
                }
        except Exception:
            pass
        return plugins, categories

    @staticmethod
    async def filter_toolset_by_entry(toolset: Any, event: Any) -> int:
        """按入口移除不允许插件的 LLM 工具。

        工具的归属插件通过 ``FunctionTool.handler_module_path`` 反查 ``star_map``；
        无法归属的工具（内置工具、MCP 工具）一律放行。

        Args:
            toolset: 待裁剪的工具集（就地修改）。
            event: 当前消息事件。

        Returns:
            被移除的工具数量。
        """
        if toolset is None or event is None:
            return 0

        from astrbot.core.star.star import star_map

        context = await PluginCategoryManager.build_context()
        entry = PluginCategoryManager.entry_of(event)
        extra_plugins, extra_categories = (
            PluginCategoryManager.extra_allowed_from_event(event)
        )

        try:
            tools = list(toolset)
        except TypeError:
            return 0

        removed = 0
        for tool in tools:
            module_path = getattr(tool, "handler_module_path", None)
            plugin_meta = star_map.get(module_path) if module_path else None
            plugin_name = getattr(plugin_meta, "name", None) if plugin_meta else None
            if not plugin_name:
                continue
            if getattr(plugin_meta, "reserved", False):
                continue
            if PluginCategoryManager.is_allowed(
                entry=entry,
                plugin_name=plugin_name,
                declared=getattr(plugin_meta, "category", None),
                context=context,
                extra_plugins=extra_plugins,
                extra_categories=extra_categories,
            ):
                continue
            try:
                toolset.remove_tool(tool.name)
                removed += 1
            except Exception as exc:  # 移除失败不应中断 LLM 请求
                logger.debug("移除工具 %s 失败：%s", getattr(tool, "name", "?"), exc)

        if removed:
            logger.debug("入口 '%s' 下按插件类别移除 %d 个 LLM 工具", entry, removed)
        return removed
