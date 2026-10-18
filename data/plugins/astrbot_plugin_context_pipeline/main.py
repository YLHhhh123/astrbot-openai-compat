"""上下文注入管线（Context Pipeline）。

接管 ``on_llm_request``，按显式阶段表依次驱动各注入插件，
取代"7 个插件各抢一个钩子、靠 priority 排队"的做法。

用法
----
1. 各被委托插件需导出 ``exported_hooks()``，返回 ``{"on_llm_request": <可调用>}``；
   未导出时管线会尝试直接调用其 ``on_llm_request`` 方法（装饰器包装后仍可调用）。
2. 本插件通过 ``on_astrbot_loaded`` 收集各插件实例并绑定到阶段。
3. WebUI「模块开关」面板可逐阶段启停，改动写入
   ``data/config/astrbot_plugin_context_pipeline_config.json``。

被委托插件被禁用/ 未安装 / 未导出钩子时，对应阶段自动跳过并记录原因，
不影响其它阶段。
"""

from __future__ import annotations

import time
from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import register

from .context_cleanup import clean_context
from .output_check import check_output
from .stages import (
    BUILTIN_STAGE_SPEC_BY_KEY,
    BUILTIN_STAGE_SPECS,
    STAGE_BY_KEY,
    STAGE_SPECS,
    PipelineContext,
    StageRegistry,
    resolve_stage_overrides,
)

PLUGIN_DIR_NAME = "astrbot_plugin_context_pipeline"


@register(
    PLUGIN_DIR_NAME,
    "WorkBuddy",
    "上下文注入管线：按显式阶段顺序驱动群境/记忆/世界书/感知等注入插件，"
    "取代多插件各抢 on_llm_request 钩子的做法，支持逐阶段开关。",
    "1.0.0",
)
class ContextPipeline(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config: dict = config if isinstance(config, dict) else {}
        self.registry = StageRegistry()
        self._enabled = True
        self._loaded = False
        self._last_states: dict[str, dict] = {}
        self._last_error: str = ""
        self._last_output_check: str = ""

    # ── 生命周期 ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        self._loaded = True
        self._enabled = bool(self.config.get("enabled", True))
        logger.info(
            f"[context_pipeline] 已加载，阶段数={len(STAGE_SPECS)} "
            f"总开关={'开' if self._enabled else '关'}"
        )
        # 先绑一次：多数场景下此时插件已全部实例化。
        # 之后 on_astrbot_loaded 还会再绑一次，两次都是幂等的。
        self.bind_all()

    async def terminate(self) -> None:
        self._loaded = False

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self, *args, **kwargs) -> None:
        """所有插件加载完毕后收集并绑定各阶段。

        ⚠️ 必须带 ``@filter.on_astrbot_loaded()`` 装饰器，否则不会被内核调用
        （早绑定会导致部分插件尚未加载完，绑定不全）。
        """
        self.bind_all()

    # ── 绑定 ────────────────────────────────────────────────────────────

    @staticmethod
    def _iter_stars():
        """遍历已加载的 star，产出 ``(plugin_id, name, 实例)`` 三元组。

        注意：``star_map`` 是 ``astrbot.core.star.star`` 的**模块级全局**，
        不是挂在 Context 上的属性；而 ``plugin_id`` 挂在 ``StarMetadata`` 上，
        插件实例自身没有，所以两者都从 metadata 里取。
        """
        try:
            from astrbot.core.star.star import star_map
        except Exception:
            return []
        out = []
        for path, meta in list(star_map.items()):
            inst = getattr(meta, "star_cls", None)
            if inst is None:
                continue
            plugin_id = getattr(meta, "plugin_id", "") or ""
            name = getattr(meta, "name", "") or ""
            out.append((plugin_id, name, inst, path))
        return out

    @staticmethod
    def _matches(target: str, plugin_id: str, name: str, path: str) -> bool:
        """阶段表里写的是插件目录名（ astrbot_plugin_xxx），匹配时按后缀比。"""
        candidates = {plugin_id, name, path.split(".")[-1]}
        # plugin_id 形如 "author/astrbot_plugin_xxx"，取后半段
        if "/" in plugin_id:
            candidates.add(plugin_id.split("/", 1)[1])
        for c in candidates:
            if not c:
                continue
            if c == target or c.endswith(f"/{target}") or c.endswith(target):
                return True
        return False

    def bind_all(self) -> dict[str, str]:
        """重新扫描并绑定所有阶段。返回 key -> 绑定结果描述。"""
        result: dict[str, str] = {}
        stars = self._iter_stars()
        for spec in STAGE_SPECS:
            target = spec.source_plugin
            found = None
            found_name = ""
            for plugin_id, name, inst, path in stars:
                if self._matches(target, plugin_id, name, path):
                    found = inst
                    found_name = name or plugin_id or path.split(".")[-1]
                    break
            if found is None:
                result[spec.key] = "未找到目标插件"
                continue
            if not self._has_hook(found, spec.hook_name):
                result[spec.key] = "目标插件未导出钩子"
                continue
            self.registry.bind(spec.key, found)
            result[spec.key] = "已绑定"
        missing = [k for k, v in result.items() if v != "已绑定"]
        if missing:
            logger.warning(f"[context_pipeline] 部分阶段未绑定：{missing}")
        else:
            logger.info("[context_pipeline] 全部阶段绑定成功")
        return result

    @staticmethod
    def _has_hook(inst, hook_name: str) -> bool:
        if hasattr(inst, "exported_hooks"):
            try:
                hooks = inst.exported_hooks()
                return bool(hooks and hook_name in hooks)
            except Exception:
                return False
        return callable(getattr(inst, hook_name, None))

    @staticmethod
    def _resolve_hook(inst, hook_name: str):
        """取出可调用的钩子；优先 ``exported_hooks()`` 提供的无装饰器版本。"""
        exported = getattr(inst, "exported_hooks", None)
        if callable(exported):
            try:
                hooks = exported() or {}
                if hook_name in hooks:
                    return hooks[hook_name]
            except Exception:
                pass
        return getattr(inst, hook_name, None)

    # ── 管线钩子 ────────────────────────────────────────────────────────

    @filter.on_llm_request()
    async def run_pipeline(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """按阶段表依次执行；只读地委托给各插件。"""
        if not self._loaded:
            return

        # 自愈：插件热重载会让弱引用失效，这里按需重新绑定一次。
        # 避免每条消息都扫全表，用stale_keys() 判空即可。
        stale = self.registry.stale_keys()
        if stale:
            self.bind_all()

        overrides = resolve_stage_overrides(self.config)
        pipe_ctx = PipelineContext()
        problems: list[str] = []

        for spec in sorted(STAGE_SPECS, key=lambda s: s.order):
            if not self._enabled:
                pipe_ctx.mark(spec.key, skipped="管线总开关关闭")
                continue
            if not overrides.get(spec.key, True):
                pipe_ctx.mark(spec.key, skipped="阶段开关关闭")
                continue

            inst = self.registry.get(spec.key)
            if inst is None:
                pipe_ctx.mark(spec.key, skipped="目标插件未绑定")
                problems.append(f"{spec.label}: 未绑定")
                continue

            hook = self._resolve_hook(inst, spec.hook_name)
            if not callable(hook):
                pipe_ctx.mark(spec.key, skipped="钩子不可用")
                problems.append(f"{spec.label}: 钩子不可用")
                continue

            t0 = time.perf_counter()
            try:
                res = hook(event, req)
                if hasattr(res, "__await__"):
                    await res
            except Exception as exc:  # 单阶段失败不拖垮整条管线
                pipe_ctx.mark(
                    spec.key,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=(time.perf_counter() - t0) * 1000,
                )
                logger.warning(f"[context_pipeline] 阶段 {spec.key} 执行失败：{exc!r}")
                continue
            pipe_ctx.mark(
                spec.key,
                ran=True,
                elapsed_ms=(time.perf_counter() - t0) * 1000,
            )

        # ── 清理上下文（管线自带阶段，order=850，插在压缩之前）─────────
        cleanup_spec = BUILTIN_STAGE_SPEC_BY_KEY.get("context_cleanup")
        if cleanup_spec is not None:
            t0 = time.perf_counter()
            if not self._enabled:
                pipe_ctx.mark(cleanup_spec.key, skipped="管线总开关关闭")
            elif not overrides.get(cleanup_spec.key, False):
                pipe_ctx.mark(cleanup_spec.key, skipped="阶段开关关闭")
            else:
                try:
                    detail = await clean_context(
                        self.context, req, config=self.config, event=event
                    )
                    pipe_ctx.mark(
                        cleanup_spec.key,
                        ran=not detail.startswith("skipped:"),
                        detail=detail,
                        elapsed_ms=(time.perf_counter() - t0) * 1000,
                    )
                    logger.debug(f"[context_pipeline] 清理上下文：{detail}")
                except Exception as exc:
                    pipe_ctx.mark(
                        cleanup_spec.key,
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_ms=(time.perf_counter() - t0) * 1000,
                    )
                    logger.warning(f"[context_pipeline] 清理上下文失败：{exc!r}")

        self._last_states = {k: v.to_dict() for k, v in pipe_ctx.states.items()}
        self._last_error = "; ".join(problems)

    # ── 输出检查（不走on_llm_request，作用于发送前的产出）─────────────

    @filter.on_decorating_result()
    async def run_output_check(self, event: AstrMessageEvent) -> None:
        """发送前检查回复内容，必要时拦截或裁剪。"""
        if not self._loaded:
            return
        mode = (self.config.get("output_check_mode") or "off").strip().lower()
        if mode == "off":
            return
        try:
            detail = await check_output(self.context, event, config=self.config)
        except Exception as exc:
            logger.warning(f"[context_pipeline] 输出检查失败（已放行）：{exc!r}")
            return
        self._last_output_check = detail
        logger.info(f"[context_pipeline] 输出检查：{detail}")

    # ── 供 WebUI 读取的状态 ─────────────────────────────────────────────

    def get_status(self) -> dict[str, Any]:
        overrides = resolve_stage_overrides(self.config)
        bound_names: dict[str, str] = {}
        for pid, nm, _inst, _path in self._iter_stars():
            for spec in STAGE_SPECS:
                if self._matches(spec.source_plugin, pid, nm, _path):
                    bound_names.setdefault(spec.key, nm or pid)
                    break
        stages = []
        for spec in sorted(STAGE_SPECS, key=lambda s: s.order):
            last = self._last_states.get(spec.key, {})
            inst = self.registry.get(spec.key)
            stages.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "order": spec.order,
                    "description": spec.description,
                    "source_plugin": spec.source_plugin,
                    "builtin": False,
                    "enabled": bool(overrides.get(spec.key, spec.enabled_default)),
                    "bound": inst is not None,
                    "bound_name": bound_names.get(spec.key, ""),
                    "last_ran": last.get("ran", False),
                    "last_skipped": last.get("skipped", ""),
                    "last_error": last.get("error", ""),
                    "last_detail": last.get("detail", ""),
                    "last_elapsed_ms": last.get("elapsed_ms", 0),
                }
            )

        # 内置阶段：清理上下文（走管线）/ 输出检查（走 on_decorating_result）
        for spec in BUILTIN_STAGE_SPECS:
            last = self._last_states.get(spec.key, {})
            if spec.key == "output_check":
                detail = self._last_output_check
                stages.append(
                    {
                        "key": spec.key,
                        "label": spec.label,
                        "order": spec.order,
                        "description": spec.description,
                        "source_plugin": "",
                        "builtin": True,
                        "hook": "on_decorating_result",
                        "enabled": (self.config.get("output_check_mode") or "off").lower()
                        != "off",
                        "bound": True,
                        "bound_name": "管线自带",
                        "last_ran": bool(detail) and not detail.startswith("skipped:"),
                        "last_skipped": detail if detail.startswith("skipped:") else "",
                        "last_error": "",
                        "last_detail": detail,
                        "last_elapsed_ms": 0,
                    }
                )
                continue
            stages.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "order": spec.order,
                    "description": spec.description,
                    "source_plugin": "",
                    "builtin": True,
                    "hook": "on_llm_request",
                    "enabled": bool(overrides.get(spec.key, spec.enabled_default)),
                    "bound": True,
                    "bound_name": "管线自带",
                    "last_ran": last.get("ran", False),
                    "last_skipped": last.get("skipped", ""),
                    "last_error": last.get("error", ""),
                    "last_detail": last.get("detail", ""),
                    "last_elapsed_ms": last.get("elapsed_ms", 0),
                }
            )

        stages.sort(key=lambda s: (s["order"], s["key"]))
        return {
            "pipeline_enabled": self._enabled,
            "loaded": self._loaded,
            "stages": stages,
            "last_problems": self._last_error,
        }

    @filter.command("上下文管线")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看上下文注入管线的阶段状态"""
        from astrbot.api.event import MessageEventResult

        st = self.get_status()
        lines = [
            f"上下文注入管线：{'总开关开' if st['pipeline_enabled'] else '总开关关'}",
            "",
        ]
        for s in st["stages"]:
            flag = "开" if s["enabled"] else "关"
            bind = "已绑定" if s["bound"] else "未绑定"
            extra = ""
            if s["last_skipped"]:
                extra = f"（上次跳过：{s['last_skipped']}）"
            elif s["last_error"]:
                extra = f"（上次出错：{s['last_error']}）"
            elif s["last_ran"]:
                extra = f"（上次 {s['last_elapsed_ms']}ms）"
            lines.append(f"  [{flag}] {s['label']:<12} {bind}{extra}")
        lines.append("")
        lines.append("用 WebUI「模块开关」面板可逐阶段启停")
        event.set_result(MessageEventResult().message("\n".join(lines)))

    @filter.command("上下文管线", alias=["重载上下文管线"])
    async def cmd_rebind(self, event: AstrMessageEvent):
        """重新绑定各阶段（目标插件刚启用/禁用后使用）"""
        from astrbot.api.event import MessageEventResult

        result = self.bind_all()
        ok = sum(1 for v in result.values() if v == "已绑定")
        lines = [f"已重新绑定 {ok}/{len(result)} 个阶段："]
        for k, v in result.items():
            label = STAGE_BY_KEY[k].label if k in STAGE_BY_KEY else k
            lines.append(f"  {label}: {v}")
        event.set_result(MessageEventResult().message("\n".join(lines)))
