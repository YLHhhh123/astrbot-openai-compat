"""插件类别与入口策略的 Dashboard 服务层。

对内核对 ``PluginCategoryManager`` 的读写做一层封装，供管理 API 使用。
"""

from __future__ import annotations

from typing import Any

from astrbot.core.core_lifecycle import AstrBotCoreLifecycle


class PluginCategoryService:
    """插件类别 / 入口策略的查询与修改。"""

    def __init__(self, core_lifecycle: AstrBotCoreLifecycle) -> None:
        self.core_lifecycle = core_lifecycle

    @staticmethod
    def _manager():
        from astrbot.core.star.plugin_category_manager import (
            CATEGORY_LABELS,
            DEFAULT_ENTRY_POLICY,
            VALID_CATEGORIES,
            PluginCategoryManager,
        )

        return (
            PluginCategoryManager,
            VALID_CATEGORIES,
            CATEGORY_LABELS,
            DEFAULT_ENTRY_POLICY,
        )

    async def list_plugins(self) -> list[dict[str, Any]]:
        """列出全部已加载插件及其类别判定结果。"""
        from astrbot.core.star.star import star_map

        (
            manager,
            valid_categories,
            labels,
            _,
        ) = self._manager()
        overrides = await manager.get_category_overrides()

        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for meta in star_map.values():
            name = getattr(meta, "name", None)
            if not name or name in seen:
                continue
            seen.add(name)
            declared = getattr(meta, "category", None)
            category = manager.resolve_category(name, declared, overrides)
            result.append(
                {
                    "name": name,
                    "display_name": getattr(meta, "display_name", None) or name,
                    "desc": getattr(meta, "desc", None) or "",
                    "version": getattr(meta, "version", None) or "",
                    "reserved": bool(getattr(meta, "reserved", False)),
                    "declared_category": declared,
                    "category": category,
                    "category_label": labels.get(category, category),
                    "source": manager.category_source(name, declared, overrides),
                }
            )
        result.sort(key=lambda item: (item["source"] != "override", item["name"]))
        return result

    async def get_meta(self) -> dict[str, Any]:
        """类别枚举与说明，供前端渲染。"""
        manager, valid_categories, labels, default_policy = self._manager()
        return {
            "categories": [
                {
                    "value": value,
                    "label": labels.get(value, value),
                    "description": (
                        "通过 LLM 工具提供能力，不依赖平台社交能力，所有入口均可用"
                        if value == "functional"
                        else "依赖平台社交能力（收发消息、@、点赞、群管理等），受限入口不可用"
                    ),
                }
                for value in valid_categories
            ],
            "default_category": "interactive",
            "default_entry_policy": default_policy,
            "entry_policy": await manager.get_entry_policy(),
        }

    async def set_plugin_category(self, plugin_name: str, category: str) -> str:
        manager, _, _, _ = self._manager()
        return await manager.set_category(plugin_name, category)

    async def clear_plugin_category(self, plugin_name: str) -> bool:
        manager, _, _, _ = self._manager()
        return await manager.clear_category(plugin_name)

    async def get_entry_policy(self) -> dict[str, Any]:
        manager, valid_categories, labels, default_policy = self._manager()
        return {
            "policy": await manager.get_entry_policy(),
            "default_policy": default_policy,
            "categories": [
                {"value": value, "label": labels.get(value, value)}
                for value in valid_categories
            ],
        }

    async def set_entry_policy(self, policy: dict[str, list[str]]) -> dict[str, Any]:
        manager, valid_categories, labels, default_policy = self._manager()
        updated = await manager.set_entry_policy(policy)
        return {
            "policy": updated,
            "default_policy": default_policy,
            "categories": [
                {"value": value, "label": labels.get(value, value)}
                for value in valid_categories
            ],
        }
