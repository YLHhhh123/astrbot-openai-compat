"""插件类别与入口策略管理器。

AstrBot 的插件按能力性质分两类：

- ``functional``（功能性）：通过 LLM 工具对外提供能力（如家居控制、天气查询），
  不依赖平台社交能力，因此在**所有入口**都可用。
- ``interactive``（互动性）：依赖平台社交能力（收发消息、@、点赞、群管理、
  主动发言等），在能力受限的入口（个人微信 openclaw、OpenAI 兼容 HTTP 接口）
  不可用。

类别来源优先级（从高到低）：

1. ``override``  —— 共享偏好里的 ``plugin_category_overrides``，用户在面板手动指定
2. ``metadata``  —— metadata.yaml 显式写 ``category: functional/interactive``
3. ``tag``       —— **由 metadata.yaml 的 ``tags`` 自动识别**（见下）
4. ``default``   —— 默认 ``functional``

第3 级是后加的：多数插件的 metadata 里只写了 ``tags`` 而没有 ``category``，
逐个补category 不现实。tags 足够表达插件性质，所以直接从它推断；
真需要精确控制时用面板手动指定覆盖。

默认类别取 ``functional``（功能性）而非 interactive：interactive 的插件依赖平台
社交能力（收发消息、@、点赞、群管理），在受限入口（个人微信 openclaw、
OpenAI 兼容 HTTP 接口）下不可用。默认给 functional 意味着"多数插件在受限入口
可用"，而真依赖社交能力的插件写 tags 即可被识别为 interactive。

tag 识别规则：把 ``tags`` 拼成小写字符串做**子串包含**匹配，先查互动词表
再查功能词表，两者都命中时取 interactive —— 社交能力是"重"能力，
错判成功能类会让它在受限入口看起来可用、实际调不动。

入口（entry）标识取事件的 ``_entry`` 扩展字段（供 HTTP 等特殊入口自报身份），
未设置时取平台**实例** id（``event.get_platform_id()``，即机器人渠道的唯一标识），
同时保留适配器类型（``platform_meta.name``）用于回退到类型级默认策略。

每个机器人渠道允许的插件类别，配置在**该机器人自己的配置项**上
（``astrbot_config["platform"][i]["allowed_plugin_categories"]``，
即 WebUI「机器人设置」里的「插件类型限制」），实现按渠道单独设置。

入口 → 允许类别的解析优先级（从高到低）：

1. 机器人实例配置 ``allowed_plugin_categories``（非空列表才生效）
2. sp 旧版入口策略，精确匹配入口 id
3. sp 旧版入口策略，按适配器类型匹配（历史面板写入的键都是类型名）
4. 内置默认策略，按适配器类型（如 weixin_oc 只放行功能性）
5. 内置默认策略 ``default``

共享偏好（``sp``）的旧版入口策略仍保留读取，兼容既有数据与
openai_compat 等不挂在机器人上的特殊入口，键名见 ``KEY_*`` 常量。
"""

from __future__ import annotations

from typing import Any

from astrbot.core import logger, sp

CATEGORY_FUNCTIONAL = "functional"
CATEGORY_INTERACTIVE = "interactive"
VALID_CATEGORIES: tuple[str, ...] = (CATEGORY_FUNCTIONAL, CATEGORY_INTERACTIVE)
DEFAULT_CATEGORY = CATEGORY_FUNCTIONAL

CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_FUNCTIONAL: "功能性",
    CATEGORY_INTERACTIVE: "互动性",
}

#: 来源标签的中文名，供面板展示。
CATEGORY_SOURCE_LABELS: dict[str, str] = {
    "override": "手动指定",
    "metadata": "metadata 声明",
    "tag": "tags 自动识别",
    "default": "默认",
}

# ── tag 识别词表─────────────────────────────────────────────────
#: 命中任一即判为互动性（依赖平台社交能力）。
INTERACTIVE_TAG_WORDS: tuple[str, ...] = (
    "互动", "社交", "聊天", "群聊", "群管理", "主动发言", "人格", "陪伴",
    "点赞", "punish", "娱乐", "game", "游戏", "music", "音乐",
    "meme", "表情包", "image", "图片", "voice", "语音", "video", "视频",
    "forward", "转发", "quote", "引用", "签到", "角色扮演", "rp",
    "chat", "talk", "group",
)

#: 命中任一即判为功能类（通过工具/接口对外提供能力）。
FUNCTIONAL_TAG_WORDS: tuple[str, ...] = (
    "功能", "工具", "tool", "utility", "llm", "模型", "model", "provider",
    "api", "web", "webui", "面板", "管理", "统计", "数据", "data",
    "备份", "backup", "性能", "performance", "监控", "monitor",
    "记忆", "memory", "缓存", "cache", "搜索", "search",
    "天气", "weather", "智能家居", "home", "http", "接口",
    "安全", "security", "压缩", "compress", "感知", "perception",
    "设定", "set", "世界书", "worldbook", "自学习", "学习", "分析",
)

# 事件的入口标识扩展字段（由 openai_compat 等特殊入口写入）
ENTRY_EXTRA_KEY = "_entry"
# HTTP 入口按 Key 额外放开的插件列表（由 openai_compat 写入）
EXTRA_ALLOWED_PLUGINS_EXTRA_KEY = "_extra_allowed_plugins"
# HTTP 入口按 Key 额外放开的类别（通常为空；留作扩展）
EXTRA_ALLOWED_CATEGORIES_EXTRA_KEY = "_extra_allowed_categories"

KEY_OVERRIDES = "plugin_category_overrides"
KEY_ENTRY_POLICY = "entry_category_policy"

# 机器人配置项上「允许的插件类别」字段名（WebUI 机器人设置 → 插件类型限制）。
# 空列表 / 缺省 = 跟随类型级内置默认。
PLATFORM_POLICY_FIELD = "allowed_plugin_categories"

ALL_CATEGORIES: list[str] = [CATEGORY_FUNCTIONAL, CATEGORY_INTERACTIVE]

# 内置默认入口策略，键为**适配器类型**（特殊入口自报的 _entry 也算类型级键）。
# 未列出的类型取 "default"，保证既有行为不变。
# 单个机器人渠道的个性化设置写在平台配置项的 PLATFORM_POLICY_FIELD 上，优先级更高。
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
    # tag 自动识别
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_tags(tags: Any) -> list[str]:
        """把 metadata 的 ``tags`` 规整成字符串列表。

        支持列表、元组、集合，以及用逗号/中文逗号分隔的单个字符串。
        """
        if tags is None:
            return []
        if isinstance(tags, str):
            parts = tags.replace("，", ",").replace(";", ",").split(",")
            return [p.strip() for p in parts if p.strip()]
        if isinstance(tags, (list, tuple, set, frozenset)):
            out: list[str] = []
            for item in tags:
                if item is None:
                    continue
                text = str(item).strip()
                if text:
                    out.append(text)
            return out
        return []

    @staticmethod
    def category_from_tags(tags: Any) -> str | None:
        """按 metadata 的 ``tags`` 推断类别；无任何命中返回 ``None``。

        先查互动词表再查功能词表：两者都命中时取 interactive。
        社交能力是"重"能力，把它错判成功能类，会让它在受限入口里
        看起来可用、实际调不动；反向错判只是少几个工具。
        """
        items = PluginCategoryManager._flatten_tags(tags)
        if not items:
            return None
        joined = " ".join(items).lower()
        for word in INTERACTIVE_TAG_WORDS:
            if word and word.lower() in joined:
                return CATEGORY_INTERACTIVE
        for word in FUNCTIONAL_TAG_WORDS:
            if word and word.lower() in joined:
                return CATEGORY_FUNCTIONAL
        return None

    # ------------------------------------------------------------------
    # 类别判定
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_category(value: Any) -> str:
        """把任意输入规整为合法类别；非法值回落默认 functional。"""
        text = str(value).strip().lower() if value is not None else ""
        if text in VALID_CATEGORIES:
            return text
        if text in ("func", "function", "tool", "工具", "功能性", "功能类"):
            return CATEGORY_FUNCTIONAL
        if text in ("inter", "interact", "chat", "互动", "互动性", "社交类"):
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
    def resolve_category(
        plugin_name: str | None,
        declared: str | None,
        overrides: dict[str, str],
        tags: Any = None,
    ) -> str:
        """按优先级解析插件类别：覆盖表 > 元数据声明 > tags 识别 > 默认。"""
        if plugin_name and plugin_name in overrides:
            return overrides[plugin_name]
        if declared:
            return PluginCategoryManager.normalize_category(declared)
        guessed = PluginCategoryManager.category_from_tags(tags)
        if guessed:
            return guessed
        return DEFAULT_CATEGORY

    @staticmethod
    def category_source(
        plugin_name: str | None,
        declared: str | None,
        overrides: dict[str, str],
        tags: Any = None,
    ) -> str:
        """类别来源：``override`` / ``metadata`` / ``tag`` / ``default``。"""
        if plugin_name and plugin_name in overrides:
            return "override"
        if declared:
            return "metadata"
        if PluginCategoryManager.category_from_tags(tags):
            return "tag"
        return "default"

    # ------------------------------------------------------------------
    # 入口策略
    # ------------------------------------------------------------------

    @staticmethod
    def _astrbot_platform_configs() -> list[dict]:
        """读取全局配置里的机器人（平台实例）配置列表。

        延迟导入 + 容错：全局配置不可用（如单测 mock 环境）时返回空列表，
        不影响既有默认策略生效。
        """
        try:
            from astrbot.core import astrbot_config
        except Exception:
            return []
        try:
            platforms = astrbot_config.get("platform", []) or []
        except Exception:
            return []
        return [item for item in platforms if isinstance(item, dict)]

    @staticmethod
    def get_instance_policies() -> dict[str, list[str]]:
        """读取每个机器人渠道上显式配置的插件类别限制。

        Returns:
            ``{机器人 id: [类别...]}``；只收录非空配置（空列表 = 跟随默认）。
        """
        out: dict[str, list[str]] = {}
        for item in PluginCategoryManager._astrbot_platform_configs():
            bot_id = str(item.get("id") or "").strip()
            if not bot_id:
                continue
            values = item.get(PLATFORM_POLICY_FIELD)
            if not isinstance(values, (list, tuple)):
                continue
            cleaned = [
                PluginCategoryManager.normalize_category(v)
                for v in values
                if str(v).strip()
            ]
            if cleaned:
                out[bot_id] = list(dict.fromkeys(cleaned))
        return out

    @staticmethod
    async def get_entry_policy() -> dict[str, list[str]]:
        """读取旧版入口策略（sp），与内置默认合并（用户配置优先）。"""
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
    def entry_identity(event: Any) -> tuple[str, str | None]:
        """解析事件所属入口，返回 ``(入口标识, 适配器类型)``。

        特殊入口（如 openai_compat）通过 ``_entry`` 自报身份，无类型信息；
        普通平台事件的入口标识是机器人实例 id（``get_platform_id()``），
        适配器类型（如 ``aiocqhttp`` / ``weixin_oc``）用于回退到类型级默认。
        """
        try:
            declared = event.get_extra(ENTRY_EXTRA_KEY)
        except Exception:
            declared = None
        if isinstance(declared, str) and declared.strip():
            return declared.strip(), None
        meta = getattr(event, "platform_meta", None)
        entry_id = str(getattr(meta, "id", "") or "").strip()
        entry_type = str(getattr(meta, "name", "") or "").strip()
        return entry_id or entry_type or "unknown", entry_type or None

    @staticmethod
    def entry_of(event: Any) -> str:
        """解析事件所属入口标识（兼容旧签名，取 ``entry_identity`` 的第一项）。"""
        return PluginCategoryManager.entry_identity(event)[0]

    @staticmethod
    def allowed_categories(
        entry: str,
        policy: dict[str, list[str]],
        *,
        entry_type: str | None = None,
        instance_policies: dict[str, list[str]] | None = None,
    ) -> set[str]:
        """解析某入口允许的插件类别集合。

        优先级：机器人实例配置 > sp 旧版策略（入口 id → 适配器类型）
        > 内置类型级默认 > 内置 ``default``。
        """
        # 1. 机器人设置里给该渠道单独配置的限制（非空才生效）
        if instance_policies:
            values = instance_policies.get(entry)
            if values:
                return set(values)
        # 2/3. sp 旧版入口策略：先精确入口 id，再按适配器类型（历史面板写入的键是类型名）
        for key in (entry, entry_type):
            if key and key in policy:
                return set(policy[key] or [])
        # 4/5. 内置默认：先按适配器类型，再落 default
        for key in (entry, entry_type, "default"):
            if key and key in DEFAULT_ENTRY_POLICY:
                return set(DEFAULT_ENTRY_POLICY[key])
        return set(ALL_CATEGORIES)

    # ------------------------------------------------------------------
    # 综合判定
    # ------------------------------------------------------------------

    @staticmethod
    async def build_context() -> dict[str, Any]:
        """一次性取齐判定所需的配置，供每轮消息过滤复用。"""
        return {
            "overrides": await PluginCategoryManager.get_category_overrides(),
            "policy": await PluginCategoryManager.get_entry_policy(),
            "instance_policies": PluginCategoryManager.get_instance_policies(),
        }

    @staticmethod
    def is_allowed(
        *,
        entry: str,
        plugin_name: str | None,
        declared: str | None,
        context: dict[str, Any],
        tags: Any = None,
        extra_plugins: set[str] | None = None,
        extra_categories: set[str] | None = None,
        entry_type: str | None = None,
    ) -> bool:
        """判定插件在给定入口下是否可用。

        Args:
            entry: 入口标识（机器人实例 id 或特殊入口自报的 ``_entry``）。
            entry_type: 入口的适配器类型，用于回退到类型级默认策略。
            plugin_name: 插件名（``None`` 时视为系统插件，放行）。
            declared: 插件元数据声明的类别。
            context: :meth:`build_context` 的结果。
            tags: 插件元数据的 ``tags``，用于自动识别类别。
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
            plugin_name, declared, context.get("overrides") or {}, tags
        )
        if extra_categories and category in extra_categories:
            return True
        allowed = PluginCategoryManager.allowed_categories(
            entry,
            context.get("policy") or {},
            entry_type=entry_type,
            instance_policies=context.get("instance_policies") or {},
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
        entry, entry_type = PluginCategoryManager.entry_identity(event)
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
                entry_type=entry_type,
                plugin_name=plugin_name,
                declared=getattr(plugin_meta, "category", None),
                context=context,
                tags=getattr(plugin_meta, "tags", None),
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
