"""多轮思考：让 AI 像人一样先想、再想、然后说。

与普通「一次性生成」的区别在于：模型先给出一版答复，再拿着这版答复问自己
「哪里不准确 / 哪里没说清 / 会不会让人误解」，据此改一版，然后才真正发出去。

为什么要分轮
------------
单轮生成时，模型在解码第一个字时就已经把话说出去了，没法回头。用「草稿 → 自检 →
改稿」两轮，等价于让模型有一次反悔机会，成本翻倍但质量明显更稳。

为什么边想边说
--------------
用户要的是"像人说话"。人是先说一句、觉得不对、再补一句，不是憋一大段再发。
所以每完成一轮思考就把这一轮的结论发出去 —— 第一版先发，后续轮次作为"补充"
接着发，形成渐进式的对话节奏。

发言间隔固定，由 ``send_interval`` 控制：人说话的间隔有节奏，但没有必要精确到
按字数模拟，固定间隔更可控（也不会因为长回答导致刷屏）。

失败必须降级，不能放弃回答
--------------------------
反思轮失败（超时/异常）时，**用上一轮草稿作为最终答复**。任何情况下都不能
因为"想太多"而让用户收不到回复。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from astrbot.core import logger

SETTINGS_KEY = "reflection_settings"

MODE_OFF = "off"
MODE_INTERNAL = "internal"   # 只发最终版
MODE_PROGRESSIVE = "progressive"  # 边想边说（默认）
MODE_WITH_THINKING = "with_thinking"  # 连思考摘要一起发

STRATEGY_HINT = {
    MODE_INTERNAL: "完整思考后只发最终答复",
    MODE_PROGRESSIVE: "每轮思考后发一条，形成渐进对话",
    MODE_WITH_THINKING: "发出思考摘要与最终答复",
}


@dataclass
class ReflectionConfig:
    enabled: bool = False
    rounds: int = 2               # 1~4；1 = 只生成不反思
    mode: str = MODE_PROGRESSIVE
    send_interval: float = 2.0    # 固定间隔（秒）
    model_group: str = ""         # 留空用默认模型分组
    timeout: float = 20.0
    max_chars_per_round: int = 0  # 0 = 不截断
    show_thinking: bool = False   # 仅 MODE_WITH_THINKING 时生效

    @classmethod
    def from_config(cls, cfg: dict | None) -> "ReflectionConfig":
        cfg = cfg if isinstance(cfg, dict) else {}
        try:
            rounds = int(cfg.get("rounds", 2))
        except (TypeError, ValueError):
            rounds = 2
        rounds = max(1, min(4, rounds))

        mode = str(cfg.get("mode") or MODE_PROGRESSIVE).strip().lower()
        if mode not in (MODE_OFF, MODE_INTERNAL, MODE_PROGRESSIVE, MODE_WITH_THINKING):
            mode = MODE_PROGRESSIVE
        if mode == MODE_OFF:
            enabled = False
        else:
            enabled = bool(cfg.get("enabled", False))

        try:
            interval = float(cfg.get("send_interval", 2.0))
        except (TypeError, ValueError):
            interval = 2.0
        interval = max(0.0, min(60.0, interval))

        try:
            timeout = float(cfg.get("timeout", 20.0))
        except (TypeError, ValueError):
            timeout = 20.0
        timeout = max(1.0, min(300.0, timeout))

        return cls(
            enabled=enabled,
            rounds=rounds,
            mode=mode,
            send_interval=interval,
            model_group=str(cfg.get("model_group") or "").strip(),
            timeout=timeout,
            max_chars_per_round=int(cfg.get("max_chars_per_round") or 0),
            show_thinking=bool(cfg.get("show_thinking", False)),
        )


@dataclass
class RoundResult:
    """单轮思考的产出。"""

    index: int
    draft: str = ""
    critique: str = ""
    revision: str = ""
    elapsed_ms: int = 0
    error: str = ""

    @property
    def text(self) -> str:
        """本轮最终可发送的文本。"""
        return (self.revision or self.draft or "").strip()

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


@dataclass
class ReflectionOutcome:
    """整体结果。``segments`` 是最终要按顺序发出的消息。"""

    segments: list[str] = field(default_factory=list)
    thinking: str = ""
    final: str = ""
    rounds: list[RoundResult] = field(default_factory=list)
    used_rounds: int = 0
    degraded: bool = False   # 是否发生过降级（某轮失败）

    def as_dict(self) -> dict[str, Any]:
        return {
            "segments": list(self.segments),
            "thinking": self.thinking,
            "final": self.final,
            "used_rounds": self.used_rounds,
            "degraded": self.degraded,
            "rounds": [
                {
                    "index": r.index,
                    "draft": r.draft,
                    "critique": r.critique,
                    "revision": r.revision,
                    "elapsed_ms": r.elapsed_ms,
                    "error": r.error,
                }
                for r in self.rounds
            ],
        }


# ── 提示词 ──────────────────────────────────────────────────────────────

FIRST_PROMPT = "请直接回答用户。"

REFLECT_PROMPT = """你刚才给出了这样一版回答：

---
{draft}
---

请检查它，然后输出一个改进后的完整回答。要求：
1. 先在心里想清楚：用户的真实意图是什么？这版回答哪里没说清、哪里可能误解？
2. 修正这些问题，保持自然口语，不要变得生硬或过于正式。
3. **只输出改进后的回答正文**，不要输出"原文""修改如下""我认为"之类的前缀。

如果这版回答已经很好，就原样输出，不要为了改而改。"""

THINKING_PROMPT = """基于下面的思考过程，用一两句话向用户说明你刚才在想什么
（像人一样，可以是"等等我想想..."这类自然过渡）。不要复述内容本身。

思考过程：
{thinking}"""

SPLIT_PROMPT = """把下面这段回答拆成2~4 条适合分开发送的消息，像聊天一样
自然分段。保持原意，不要新增内容。严格用如下 JSON 格式输出，不要任何其他文字：

{{"messages": ["第一条", "第二条"]}}"""

SEGMENT_SPLIT_RE = None  # 保留位：解析走 JSON，容错见 _split_fallback


def build_first_prompt() -> str:
    return FIRST_PROMPT


def build_reflect_prompt(draft: str) -> str:
    return REFLECT_PROMPT.format(draft=draft[:4000])


# ── 反思执行 ────────────────────────────────────────────────────────────


async def reflect(
    call_model,
    user_input: str,
    cfg: ReflectionConfig,
    context_text: str = "",
) -> ReflectionOutcome:
    """执行多轮思考。

    Args:
        call_model: 异步可调用 ``(prompt: str, timeout: float) -> str``，
            由调用方注入（内部走 ``llm_generate`` 或模型分组）。
        user_input: 用户这一轮说的话。
        cfg: 配置。
        context_text: 可选的附加上下文（如对话摘要）。

    Returns:
        :class:`ReflectionOutcome`；``cfg.enabled`` 为假时返回空结果，
        调用方据此走原有流程。
    """
    out = ReflectionOutcome()
    if not cfg.enabled or cfg.rounds < 2:
        return out

    payload = f"{user_input}\n\n{context_text}" if context_text else user_input
    draft = ""
    thoughts: list[str] = []

    for i in range(1, cfg.rounds + 1):
        t0 = asyncio.get_event_loop().time()
        rr = RoundResult(index=i)

        # 第 1 轮：直接生成草稿；之后各轮拿上一轮草稿去改
        if i == 1:
            prompt = f"{build_first_prompt()}\n\n{payload}"
        else:
            prompt = build_reflect_prompt(draft)

        try:
            raw = await call_model(prompt, cfg.timeout)
        except Exception as exc:
            rr.error = f"{type(exc).__name__}: {exc}"
            rr.elapsed_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
            out.rounds.append(rr)
            out.degraded = True
            logger.warning(
                f"[reflection] 第 {i} 轮失败，沿用上一版：{rr.error}"
            )
            break

        text = _clean(raw, cfg)
        if not text:
            rr.error = "模型返回空"
            rr.elapsed_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
            out.rounds.append(rr)
            out.degraded = True
            break

        if i == 1:
            rr.draft = text
            draft = text
        else:
            rr.draft = draft
            rr.revision = text
            thoughts.append(text)
            draft = text

        rr.elapsed_ms = int((asyncio.get_event_loop().time() - t0) * 1000)
        out.rounds.append(rr)
        out.used_rounds = i

    if not draft:
        return out

    out.final = draft
    out.thinking = "\n".join(thoughts)

    if cfg.mode == MODE_INTERNAL:
        out.segments = [draft]
    elif cfg.mode == MODE_WITH_THINKING and out.thinking:
        out.segments = [out.thinking, draft]
    elif cfg.mode == MODE_PROGRESSIVE and out.used_rounds >= 2:
        # 边想边说：把每一轮的产出都作为一条消息，天然形成渐进感
        out.segments = _progressive_segments(out, cfg)
    else:
        out.segments = [draft]

    return out


def _progressive_segments(out: ReflectionOutcome, cfg: ReflectionConfig) -> list[str]:
    """渐进式分段：每轮一条，去掉与上一条重复的开头。"""
    parts: list[str] = []
    for rr in out.rounds:
        text = (rr.text or "").strip()
        if not text:
            continue
        if parts and _is_prefix_redundant(text, parts[-1]):
            # 改写版常常是"完整替换"而不是"补充"，整段追加会显得重复
            continue
        parts.append(text)
    return parts or [out.final]


def _is_prefix_redundant(new_text: str, prev: str) -> bool:
    """判断新一段是否只是旧一段的重复延伸。

    早期实现是「新段前 12 字是否出现在旧段里」，但改写版经常在复述一小段后
    继续往下写（如「我先想想怎么说……其实该直接回答你」），前缀会跨过旧段的
    结尾，字符串包含判断必然失配。

    改为按**开头词的重合度**判断：取较短文本的所有二元词组，看新段开头有多少
    能在旧段里找到。重合过半即视为重复。
    """
    a, b = new_text.strip(), prev.strip()
    if not a or not b:
        return False

    head = a[: min(len(a), 12)]
    if len(head) < 6:
        return False

    bigrams = {head[i : i + 2] for i in range(len(head) - 1)}
    if not bigrams:
        return False
    hit = sum(1 for g in bigrams if g in b)
    return hit / len(bigrams) >= 0.5


def _clean(raw: str | None, cfg: ReflectionConfig) -> str:
    """清理模型输出：去围栏、裁长度。"""
    if not raw:
        return ""
    text = str(raw).strip()
    if text.startswith("```"):
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[2] if parts[0].strip() == "" else parts[1]
        else:
            text = text.strip("`")
    text = text.strip()
    if cfg.max_chars_per_round and len(text) > cfg.max_chars_per_round:
        text = text[: cfg.max_chars_per_round].rstrip() + "…"
    return text


async def split_into_messages(call_model, text: str, cfg: ReflectionConfig) -> list[str]:
    """把一段长文本拆成多条适合分开发送的消息。

    失败时返回原文本 —— 拆分是锦上添花，不该让回复发不出去。
    """
    if not text or len(text) < 60:
        return [text] if text else []
    try:
        raw = await call_model(SPLIT_PROMPT.format() if False else SPLIT_PROMPT + "\n\n" + text[:4000], cfg.timeout)
    except Exception as exc:
        logger.debug(f"[reflection] 分段失败，保持原文：{exc}")
        return [text]

    msgs = _parse_split(raw)
    return msgs if msgs else [text]


def _parse_split(raw: str | None) -> list[str]:
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1] if len(parts) >= 2 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.split("```")[0].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list):
        return []
    out = [str(m).strip() for m in msgs if str(m).strip()]
    return out if len(out) >= 2 else []