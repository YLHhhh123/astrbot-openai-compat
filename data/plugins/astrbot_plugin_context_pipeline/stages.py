"""上下文注入管线 —— 阶段定义与调度。

设计要点
--------
原先有 7 个插件各自注册 ``on_llm_request``，靠 ``priority`` 大小排队：

    prompt_compress                priority=-1000  （必须最后跑，做压缩）
    group_context                  priority=-200
    cross_group_memory             priority=-150
    customized_active_reply_prompt priority=-100
    worldbook                       默认（0）
    LLMPerception                   默认（0）
    self_learning                   默认（0）

这套排序是**隐式且脆弱**的：新增插件一旦忘了看 priority 表就会插错位置，
而 ``prompt_compress`` 必须最后跑这个约束没有任何机制保障。

本模块把顺序变成**显式阶段表**，并由调度器按序调用各阶段。

实现方式：**委托而非搬移**
--------------------------------
各阶段的业务逻辑（缓冲池、记忆检索、世界书匹配…）都依赖各自插件的内部状态，
搬进一个进程会让7 套状态纠缠在一起，反而更难维护。所以本插件：

1. 每个阶段持有一个对目标插件实例的弱引用；
2. 只在管线钩子里按阶段表**依次调用**它的 ``on_llm_request``；
3. 被委托的插件需把自己的钩子导出为``exported_hooks()``（见各插件改造）。

这样顺序由本插件集中管理，配置与开关也集中，而业务代码仍留在原处。
目标插件被禁用或未安装时，该阶段自动跳过并在WebUI 标注原因。
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass, field

# 注入块标记：与被合并的插件保持完全一致，prompt_compress 依赖它们定位文本块。
# ⚠️ 不要随意改动，否则压缩阶段会找不到要压缩的内容。
GC_START_MARK = "--- BEGIN GROUP CONTEXT ---"
GC_END_MARK = "--- END GROUP CONTEXT ---"
CGM_START_MARK = "[跨群记忆]"
CGM_END_MARK = "[跨群记忆结束]"


@dataclass
class StageSpec:
    """一个阶段的静态定义。"""

    key: str
    label: str
    order: int
    source_plugin: str
    description: str = ""
    enabled_default: bool = True
    #: 目标插件实例应导出的方法名（缺省即同名 ``on_llm_request``）
    hook_name: str = "on_llm_request"


@dataclass
class StageState:
    """一个阶段的运行时状态。"""

    key: str
    ran: bool = False
    skipped: str = ""
    error: str = ""
    elapsed_ms: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "ran": self.ran,
            "skipped": self.skipped,
            "error": self.error,
            "elapsed_ms": round(self.elapsed_ms, 2),
            "detail": self.detail,
        }


# ── 阶段表───────────────────────────────────────────────────────────────
# order 即执行顺序，勿随意调整；900 之后留给压缩。
STAGE_SPECS: list[StageSpec] = [
    StageSpec(
        key="group_context",
        label="群聊语境",
        order=100,
        source_plugin="astrbot_plugin_group_context",
        description="采集群内近期消息作为语境，消费式注入（注入过的记录不再重复）",
    ),
    StageSpec(
        key="cross_group_memory",
        label="跨群记忆",
        order=200,
        source_plugin="astrbot_plugin_cross_group_memory",
        description="检索该用户在其他群的记忆，跨群互通",
    ),
    StageSpec(
        key="self_learning",
        label="自学习",
        order=300,
        source_plugin="astrbot_plugin_self_learning",
        description="按群/话题注入历史学到的条目",
    ),
    StageSpec(
        key="worldbook",
        label="世界书",
        order=400,
        source_plugin="astrbot_plugin_worldbook",
        description="群级设定条目的匹配与注入",
    ),
    StageSpec(
        key="llm_perception",
        label="时间感知",
        order=500,
        source_plugin="astrbot_plugin_LLMPerception",
        description="注入当前时间、节日等环境信息",
    ),
    StageSpec(
        key="active_reply_prompt",
        label="主动回复提示词",
        order=600,
        source_plugin="astrbot_plugin_customized_active_reply_prompt",
        description="群聊主动回复时替换提示词（须在记忆处理之后）",
    ),
    StageSpec(
        key="prompt_compress",
        label="注入压缩",
        order=900,
        source_plugin="astrbot_plugin_prompt_compress",
        description="对已注入内容做截断/摘要，降低 token；必须最后执行",
    ),
]

# ── 本管线自带的两个可选功能（不委托给别的插件）───────────────────────
# 它们由本插件直接实现，故source_plugin 为空字符串，且默认关闭：
# 清理上下文与输出检查都要额外调一次 LLM，属于「按需开启」的成本项。

BUILTIN_STAGE_SPECS: list[StageSpec] = [
    StageSpec(
        key="context_cleanup",
        label="清理上下文",
        order=850,
        source_plugin="",  # 本插件自带
        description="用小模型把过长的注入内容重写成精炼版本（不截断语义）",
        enabled_default=False,
    ),
    StageSpec(
        key="output_check",
        label="检查输出",
        order=1000,  # 仅用于面板排序（实际不走管线钩子，见 output_check.py）
        source_plugin="",
        description="发送前检查回复内容，可按规则拦截或用小模型复核",
        enabled_default=False,
    ),
]

# output_check 作用于「回复产出」而非「请求注入」，不参与阶段排序执行；
# 为避免混入 on_llm_request 阶段表，这里单独存放。
REQUEST_STAGE_SPECS = STAGE_SPECS

# 清理上下文要插在压缩（900）之前，所以它需要进请求阶段表
BUILTIN_STAGE_SPEC_BY_KEY = {s.key: s for s in BUILTIN_STAGE_SPECS}

STAGE_BY_KEY = {s.key: s for s in STAGE_SPECS}


@dataclass
class PipelineContext:
    """管线运行上下文，传递给各阶段的包装层。"""

    states: dict[str, StageState] = field(default_factory=dict)

    def mark(self, key: str, **kw) -> None:
        st = self.states.setdefault(key, StageState(key=key))
        for k, v in kw.items():
            setattr(st, k, v)


class StageRegistry:
    """持有各阶段目标插件实例的引用。

    用**弱引用**避免阻碍插件热重载时旧实例被回收；但弱引用一旦失效，
    ``get()`` 会返回 None，调度器据此跳过该阶段并提示"未绑定"，
    调用方可用 ``bind_all()`` 重新绑定（WebUI 面板与 ``/上下文管线 重载`` 都提供入口）。

    之所以不缓存强引用：插件热重载会替换实例，强引用会让旧插件的
    缓冲池/记忆索引等状态长期驻留，内存只增不减。
    """

    def __init__(self) -> None:
        self._refs: dict[str, weakref.ReferenceType] = {}
        # 阶段 → 该插件上真实存在的钩子方法名（见 record_hook_name）
        self._hook_names: dict[str, str] = {}

    def record_hook_name(self, key: str, hook_name: str) -> None:
        """记录该阶段实际使用的钩子方法名。

        阶段表里的 ``hook_name`` 只是默认约定；真实插件的方法名五花八门
        （my_custom_hook_1 / replace_reply_prompt / compress_llm_request），
        绑定时以探测结果为准。
        """
        self._hook_names[key] = hook_name

    def get_hook_name(self, key: str, default: str = "on_llm_request") -> str:
        return self._hook_names.get(key, default)

    def bind(self, key: str, instance) -> None:
        try:
            self._refs[key] = weakref.ref(instance)
        except TypeError:
            # 不可弱引用的对象（少见）→ 退化为恒定返回
            self._refs[key] = lambda inst=instance: inst

    def get(self, key: str):
        ref = self._refs.get(key)
        return ref() if ref is not None else None

    def bound_keys(self) -> list[str]:
        return [k for k, ref in self._refs.items() if ref() is not None]

    def stale_keys(self) -> list[str]:
        """返回"登记过但实例已消失"的阶段 key —— 即需要重新绑定的。"""
        return [k for k, ref in self._refs.items() if ref() is None]


def resolve_stage_overrides(config: dict) -> dict[str, bool]:
    """从配置里读出各阶段的启停状态。

    配置用**平铺键** ``stage_<key>``（内核 schema 不支持嵌套 object，
    早期版本用 ``stages: {...}`` 的写法仍兼容读取）::

        {"stage_worldbook": false, "stages": {"group_context": false}}

    未显式配置的阶段用 ``StageSpec.enabled_default``。

    ⚠️ 必须同时遍历 ``STAGE_SPECS``（委托阶段）与 ``BUILTIN_STAGE_SPECS``
    （管线自带的清理上下文），否则内置阶段永远拿不到配置、恒为默认关闭。
    """
    cfg = config if isinstance(config, dict) else {}
    nested = cfg.get("stages")
    nested = nested if isinstance(nested, dict) else {}

    out: dict[str, bool] = {}
    for spec in (*STAGE_SPECS, *BUILTIN_STAGE_SPECS):
        val = None
        if f"stage_{spec.key}" in cfg:
            val = cfg[f"stage_{spec.key}"]
        elif spec.key in nested:
            val = nested[spec.key]
        out[spec.key] = spec.enabled_default if val is None else bool(val)
    return out


def stage_config_key(key: str) -> str:
    """阶段在配置文件里的键名。"""
    return f"stage_{key}"
