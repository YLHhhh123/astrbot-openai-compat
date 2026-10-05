"""外部工具（由客户端执行）支持。

携带客户端 ``tools`` 的请求走 AstrBot 管道时，这些工具被登记为「外部工具」：

- **参与** LLM 的 function calling（模型能看到并可以调用它们）；
- 服务端**不执行** —— 调用被拦截，原样回传给调用方（HTTP 层），由客户端执行。

分流策略（S3）
--------------
- 一轮内**只有**外部工具调用 → 中断本轮 loop，把待执行的调用交还调用方；
- 一轮内**混合**了服务端工具与外部工具 → 服务端工具照常执行并继续 loop，
  外部工具写入明确的占位结果（引导模型「下一轮单独调用」），
  从而让后续轮次自然收敛为「纯外部轮」，保证客户端拿到调用时服务端工具的结果
  已被模型消化完毕，上下文不会错位。

工具名单通过事件扩展字段在管道内传递，键名见
``EXTERNAL_TOOL_NAMES_EXTRA_KEY``。
"""

from __future__ import annotations

from typing import Any

EXTERNAL_TOOL_NAMES_EXTRA_KEY = "_external_tool_names"
"""事件扩展字段：本轮登记的外部工具名列表。"""

EXTERNAL_TOOL_DEFS_EXTRA_KEY = "_external_tool_defs"
"""事件扩展字段：客户端工具的 OpenAI 定义（原样保存，供管道合并）。"""


class ExternalToolCallPending(Exception):
    """一轮内出现外部工具调用，需中断本轮 loop 并交给客户端执行。

    Attributes:
        calls: 待客户端执行的调用列表，元素形如
            ``{"id": ..., "name": ..., "arguments": {...}}``。
    """

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        super().__init__("external tool calls pending")
        self.calls = calls


def register_external_tools(event: Any, names: list[str] | None) -> None:
    """在事件上登记外部工具名，供 runner 判定归属。"""
    if event is None:
        return
    cleaned = [str(name).strip() for name in (names or []) if str(name).strip()]
    if not cleaned:
        return
    try:
        event.set_extra(EXTERNAL_TOOL_NAMES_EXTRA_KEY, cleaned)
    except Exception:
        pass


def external_tool_names_of(event: Any) -> set[str]:
    """读取事件上登记的外部工具名集合。"""
    if event is None:
        return set()
    try:
        raw = event.get_extra(EXTERNAL_TOOL_NAMES_EXTRA_KEY)
    except Exception:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(item) for item in raw if str(item).strip()}
    return set()


def event_of(run_context: Any) -> Any:
    """从运行上下文安全取出事件；runner 无事件时返回 ``None``。

    与 ``tool_loop_agent_runner`` 内部既有写法保持一致。
    """
    context = getattr(run_context, "context", None)
    return getattr(context, "event", None)


def build_calls(
    names: list[str],
    args: list[Any],
    ids: list[str],
) -> list[dict[str, Any]]:
    """把并列的工具调用字段组装成调用列表。"""
    calls: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        calls.append(
            {
                "id": str(ids[index]) if index < len(ids) else f"call_{index}",
                "name": str(name),
                "arguments": args[index] if index < len(args) else {},
            }
        )
    return calls


def register_external_tool_defs(event: Any, defs: list[dict] | None) -> None:
    """在事件上保存客户端工具的 OpenAI 定义，供管道合并进工具集。"""
    if event is None or not isinstance(defs, list) or not defs:
        return
    cleaned = [item for item in defs if isinstance(item, dict)]
    if not cleaned:
        return
    try:
        event.set_extra(EXTERNAL_TOOL_DEFS_EXTRA_KEY, cleaned)
    except Exception:
        pass


def external_tool_defs_of(event: Any) -> list[dict]:
    """读取事件上保存的客户端工具定义。"""
    if event is None:
        return []
    try:
        raw = event.get_extra(EXTERNAL_TOOL_DEFS_EXTRA_KEY)
    except Exception:
        return []
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def tool_names_of(tools: Any) -> list[str]:
    """从 OpenAI tools 定义中提取工具名（保序、去空）。"""
    names: list[str] = []
    if not isinstance(tools, list):
        return names
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        spec = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = str(spec.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def build_toolset(tools: Any) -> Any | None:
    """把 OpenAI tools 定义转成 AstrBot ``ToolSet``（**不绑定执行者**）。

    管道侧（合并进 LLM 工具集）与 HTTP 侧（直连 provider）共用同一份实现。
    非法定义静默跳过，不阻断其余工具。
    """
    if not isinstance(tools, list) or not tools:
        return None

    from .tool import FunctionTool, ToolSet

    toolset = ToolSet()
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        spec = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        name = str(spec.get("name") or "").strip()
        if not name:
            continue
        parameters = spec.get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            parameters = {"type": "object", "properties": {}}
        try:
            toolset.add_tool(
                FunctionTool(
                    name=name,
                    description=str(spec.get("description") or ""),
                    parameters=parameters,
                )
            )
        except Exception:
            continue
    return toolset if len(toolset) else None
