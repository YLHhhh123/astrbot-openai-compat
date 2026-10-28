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

import os
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
    "YLHhhh123",
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
        """阶段表里写的是插件目录名（ astrbot_plugin_xxx），匹配时按后缀比。

        ``plugin_id`` 在 4.28.x 的 ``StarMetadata`` 上**并不存在**——字段只有
        name / author / desc / short_desc / version / repo / star_cls_type，
        getattr 拿到的是空串。原先把它当主要候选，于是四个插件全部匹配失败。

        因此匹配以 ``name`` 与模块路径 ``path`` 为主：路径末两段都要试，因为
        单模块插件是 ``astrbot_plugin_x.main``、多模块插件是
        ``astrbot_plugin_x.core.framework_llm_adapter``，末段都是 ``main``/
        业务模块名，光看末段匹配不到。plugin_id 只在万一存在时作为补充。
        """
        candidates = {name}
        parts = path.split(".")
        # 路径里第一个以 astrbot_plugin_ 开头的段就是插件目录名，最可靠
        for seg in parts:
            if seg.startswith("astrbot_plugin_"):
                candidates.add(seg)
        for depth in (1, 2, 3):
            if len(parts) >= depth:
                candidates.add(parts[-depth])
        if plugin_id:
            candidates.add(plugin_id)
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
                # 兜底：star_map 里没有就直接 import。
                # self_learning 属于这种——目录无 astrbot_plugin_ 前缀、
                # 类上也没有 @register，内核从未把它放进 star_map。
                imported = ContextPipeline._stars_by_import()
                logger.info(
                    f"[context_pipeline] 兜底扫描得到 {len(imported)} 个："
                    + ", ".join(sorted(imported)[:15])
                )
                for entry, cls in imported.items():
                    if ContextPipeline._same_plugin(entry, target):
                        found = cls
                        found_name = entry
                        break
            if found is None:
                result[spec.key] = "未找到目标插件"
                continue
            if not self._has_hook(found, spec.hook_name):
                # 阶段表里的 hook_name 只是约定；真实插件的方法名可能是
                # my_custom_hook_1 / replace_reply_prompt / compress_llm_request
                # / inject_diversity_to_llm_request。靠探测而不是穷举名字。
                real = self._find_hook_inst(found)
                if not real:
                    cands = [
                        a for a in dir(found)
                        if not a.startswith("__")
                        and callable(getattr(found, a, None))
                        and a not in ("terminate", "initialize")
                    ]
                    logger.warning(
                        f"[context_pipeline] key={spec.key} 找到插件但探测不到钩子；"
                        f"类={found}；可调用方法候选={cands[:12]}"
                    )
                    result[spec.key] = "目标插件未导出钩子"
                    continue
            else:
                real = self._find_hook_inst(found) or spec.hook_name
            self.registry.bind(spec.key, found)
            self.registry.record_hook_name(spec.key, real)
            result[spec.key] = "已绑定"
        missing = [k for k, v in result.items() if v != "已绑定"]
        if missing:
            logger.warning(f"[context_pipeline] 部分阶段未绑定：{missing}")
            # 把实际扫到的候选打出来，否则面板上只显示"未绑定"，
            # 现场无从判断是插件没加载、名字对不上，还是钩子名不对。
            for k in missing:
                target = next(
                    (s.source_plugin for s in STAGE_SPECS if s.key == k), ""
                )
                logger.info(
                    f"[context_pipeline] 未绑定诊断 key={k} target={target} "
                    f"扫描到 {len(stars)} 个 Star："
                    + ", ".join(f"{n or '?'}({p})" for _pid, n, _i, p in stars[:20])
                )
        else:
            logger.info("[context_pipeline] 全部阶段绑定成功")
        return result

    @staticmethod
    def _same_plugin(entry: str, target: str) -> bool:
        """比较插件标识，忽略 astrbot_plugin_ 前缀。

        服务器上的插件目录**普遍没有前缀**（group_context / self_learning /
        worldbook），而阶段表里写的是带前缀的 astrbot_plugin_xxx。
        两侧都剥掉前缀再比。
        """

        def bare(x: str) -> str:
            x = str(x or "").strip()
            return x[len("astrbot_plugin_"):] if x.startswith("astrbot_plugin_") else x

        a, b = bare(entry), bare(target)
        return bool(a) and a == b

    @staticmethod
    def _stars_by_import():
        """star_map 里没有时，直接按插件目录名 import 它的 main 模块。

        有些插件（实测是self_learning）没有 @register 装饰器、也没进 star_map，
        但仍然装着可用的 LLM 请求钩子。只看 star_map 会漏掉它们。
        返回 name -> 类。
        """
        out: dict[str, Any] = {}
        plugin_root = None
        # 数据目录的取法：astrbot.core.data_path 并不存在（4.28 上会 ImportError），
        # 正确的是 utils.astrbot_path.get_astrbot_data_path()。
        for getter in (
            lambda: __import__(
                "astrbot.core.utils.astrbot_path", fromlist=["get_astrbot_data_path"]
            ).get_astrbot_data_path(),
            lambda: __import__(
                "astrbot.core", fromlist=["get_astrbot_data_path"]
            ).get_astrbot_data_path(),
        ):
            try:
                plugin_root = os.path.join(str(getter()), "plugins")
                if os.path.isdir(plugin_root):
                    break
                plugin_root = None
            except Exception:
                continue
        if not plugin_root or not os.path.isdir(plugin_root):
            logger.warning("[context_pipeline] 找不到插件目录，兜底扫描跳过")
            return out
        try:
            entries = sorted(os.listdir(plugin_root))
        except Exception:
            return out
        for entry in entries:
            # 不限制前缀：实测self_learning 的目录就叫 "self_learning"，
            # 没有 astrbot_plugin_ 前缀，按前缀过滤会直接漏掉它。
            if entry.startswith("_") or entry.startswith("."):
                continue
            if not os.path.isfile(os.path.join(plugin_root, entry, "main.py")):
                continue
            if entry in out:
                continue
            mod_name = f"data.plugins.{entry}.main"
            try:
                import importlib

                mod = importlib.import_module(mod_name)
            except Exception:
                continue
            for attr in dir(mod):
                obj = getattr(mod, attr, None)
                if not isinstance(obj, type):
                    continue
                try:
                    from astrbot.api.star import Star as _Star
                except Exception:
                    return out
                if issubclass(obj, _Star) and obj is not _Star:
                    out[entry] = obj
                    break
        return out

    @staticmethod
    def _find_hook_inst(cls):
        """找出该插件类上真正承载 LLM 请求钩子的方法名，找不到返回 None。

        **不能假设方法名是 on_llm_request**—— 那是装饰器的名字，不是函数名。
        实测服务器上的插件五花八门：

            LLMPerception                    -> my_custom_hook_1
            customized_active_reply_prompt    -> replace_reply_prompt
            prompt_compress                  -> compress_llm_request
            self_learning                    -> on_llm_request（恰好同名）

        装饰器 register_on_llm_request 会把函数登记进 handler 表并原样返回
        函数对象，所以可靠判据是"该方法带 OnLLMRequestEvent 的 handler"。
        """
        if cls is None:
            return None

        # 首选：查 handler 表里的事件类型，最准确
        try:
            from astrbot.core.star.register.star_handler import get_handler

            for attr in dir(cls):
                if attr.startswith("__"):
                    continue
                try:
                    member = getattr(cls, attr, None)
                except Exception:
                    continue
                h = get_handler(member)
                if h is None:
                    continue
                etype = str(getattr(h, "event_type", "") or "")
                if "OnLLMRequest" in etype:
                    return attr
        except Exception:
            pass

        # 退路一：同名
        for attr in dir(cls):
            if attr.lower() == "on_llm_request":
                return attr

        # 退路二：按命名特征猜（服务器上实测的三种命名都在此覆盖内）
        for attr in dir(cls):
            if attr.startswith("__"):
                continue
            low = attr.lower()
            if not callable(getattr(cls, attr, None)):
                continue
            has_kw = any(k in low for k in ("llm", "prompt", "compress", "hook"))
            # 前缀白名单覆盖服务器上实测到的全部命名，包括
            # self_learning 的 inject_diversity_to_llm_request
            good_prefix = low.startswith(
                (
                    "my_", "replace_", "compress_", "handle_", "do_", "on_",
                    "build_", "inject_", "apply_", "append_", "add_",
                    "before_", "pre_", "process_", "decorate_", "enrich_",
                )
            )
            if has_kw and good_prefix:
                return attr
        return None

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
        direct = getattr(inst, hook_name, None)
        if callable(direct):
            return direct
        # 名字对不上时按探测结果取：真实插件的钩子方法名并不统一
        # （my_custom_hook_1 / replace_reply_prompt / compress_llm_request）。
        found = ContextPipeline._find_hook_inst(inst)
        if found:
            return getattr(inst, found, None)
        return None

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

            hook = self._resolve_hook(
                inst, self.registry.get_hook_name(spec.key, spec.hook_name)
            )
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
