"""用户形象推理：从聊天记录与互动记录里推出一个人的形象。

取材
----
来自 ``platform_message_history``：谁在什么群里说了什么。之所以只用自己的
消息作为主要素材，是因为**对话里最能看出性格的是这个人自己怎么说**（用词、
句长、爱不爱接话、什么时候冒泡），而不是别人怎么评价他 —— 后者容易把
"别人怎么看他"混进"他是什么样的人"。

同时统计互动特征作为客观佐证：发言量、平均句长、夜间发言占比、被@次数、
提问频率。这些是可量化的，不依赖模型判断，可以直接写进 prompt当事实。

不出结论的情况
--------------
样本不足时**直接放弃推理**，返回 ``ok=False`` 并说明原因，而不是让模型从三条
消息里编一个"他是内向的人"。宁可没有，不可编造。

失败不覆盖
----------
推理失败或结果不可用时，保留旧画像不动 —— 一个空画像比一个错画像更安全，
而错画像会被后续所有用到画像的功能当真。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.core import logger

from .models import (
    DEFAULT_MIN_SAMPLES,
    effective_confidence,
    parse_llm_result,
    sufficient,
)

SETTINGS_KEY = "persona_settings"

PROMPT = """你正在根据一个人的真实聊天记录，概括他在群聊里的形象。

## 客观统计（这些是事实，可以直接采信）
{stats}

## 他的发言摘录
{samples}

## 你的任务
综合以上，输出一份形象描述。要求：

1. **summary**：一句话概括他是个什么样的人（不超过 30 字），例如"爱接梗的活跃分子，偶尔冷场救场"。
2. **traits**：分维度给标签，维度建议用「性格」「习惯」「情绪」「社交角色」，每维2~5 个。
3. **evidence**：从上面的摘录里挑 3~5 条最能支撑你结论的原话，**必须是原话片段**，
   不要改写、不要编造。没有把握就不要给。

严格输出以下 JSON，不要任何其他文字：
{{"summary":"...","traits":{{"性格":["..."],"习惯":["..."]}},"evidence":["原话片段1","原话片段2"]}}"""


@dataclass
class PersonaSettings:
    enabled: bool = False
    idle_minutes: int = 10
    min_samples: int = DEFAULT_MIN_SAMPLES
    max_per_day: int = 3
    model_group: str = ""
    max_messages: int = 120
    timeout: float = 45.0
    sample_turns: int = 40

    @classmethod
    def from_config(cls, cfg: dict | None) -> "PersonaSettings":
        cfg = cfg if isinstance(cfg, dict) else {}

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            try:
                v = int(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        def _float(key: str, default: float, lo: float, hi: float) -> float:
            try:
                v = float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        return cls(
            enabled=bool(cfg.get("enabled", False)),
            idle_minutes=_int("idle_minutes", 10, 1, 1440),
            min_samples=_int("min_samples", DEFAULT_MIN_SAMPLES, 1, 10000),
            max_per_day=_int("max_per_day", 3, 1, 100),
            model_group=str(cfg.get("model_group") or "").strip(),
            max_messages=_int("max_messages", 120, 10, 2000),
            timeout=_float("timeout", 45.0, 1.0, 600.0),
            sample_turns=_int("sample_turns", 40, 5, 400),
        )


@dataclass
class InferenceResult:
    ok: bool = False
    reason: str = ""
    summary: str = ""
    traits: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    confidence: float = 0.0
    sample_count: int = 0
    elapsed_ms: int = 0
    model_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "summary": self.summary,
            "traits": self.traits,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "elapsed_ms": self.elapsed_ms,
            "model_id": self.model_id,
        }


# ── 取材 ────────────────────────────────────────────────────────────────


def collect_messages(rows: list[Any], limit: int = 120) -> list[dict]:
    """把数据库行整理成可读的发言列表（取最近 ``limit`` 条）。"""
    out: list[dict] = []
    for row in rows[-limit:]:
        text = _extract_text(getattr(row, "content", None))
        if not text:
            continue
        out.append(
            {
                "text": text,
                "sender_id": getattr(row, "sender_id", None),
                "sender_name": getattr(row, "sender_name", None),
                "user_id": getattr(row, "user_id", None),
                "platform_id": getattr(row, "platform_id", None),
                "at": getattr(row, "created_at", None) or getattr(row, "id", None),
                "llm_checkpoint_id": getattr(row, "llm_checkpoint_id", None),
            }
        )
    return out


def _extract_text(content: Any) -> str:
    """从消息链 JSON 里取出纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, dict):
        for key in ("plain", "text", "content", "message"):
            if key in content:
                v = content[key]
                if isinstance(v, str) and v.strip():
                    return v.strip()
        # 消息链：挑第一段纯文本
        chain = content.get("chain") or content.get("message_chain")
        if isinstance(chain, list):
            for seg in chain:
                if isinstance(seg, dict):
                    t = seg.get("text")
                    if isinstance(t, str) and t.strip():
                        return t.strip()
        return ""
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, dict):
                t = seg.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
            elif isinstance(seg, str) and seg.strip():
                parts.append(seg.strip())
        return " ".join(parts)
    return ""


def compute_stats(messages: list[dict]) -> dict[str, Any]:
    """算出客观统计量，作为 prompt 里的事实依据。"""
    n = len(messages)
    if n == 0:
        return {"total": 0}

    lengths = [len(m["text"]) for m in messages if m.get("text")]
    avg_len = round(sum(lengths) / len(lengths), 1) if lengths else 0
    questions = sum(1 for m in messages if "?" in m["text"] or "？" in m["text"])
    laughs = sum(
        1
        for m in messages
        if any(k in m["text"] for k in ("哈", "笑", "草", "lol", "233", "hhh"))
    )
    short = sum(1 for m in messages if len(m["text"]) <= 5)

    nights = 0
    day_total = 0
    for m in messages:
        ts = m.get("at")
        if not isinstance(ts, (int, float)):
            continue
        hour = time.localtime(float(ts)).tm_hour
        day_total += 1
        if hour >= 23 or hour < 6:
            nights += 1
    night_ratio = round(nights / day_total * 100) if day_total else 0

    groups = {m.get("user_id") for m in messages if m.get("user_id")}

    return {
        "total": n,
        "avg_len": avg_len,
        "question_ratio": round(questions / n * 100),
        "laugh_ratio": round(laughs / n * 100),
        "short_ratio": round(short / n * 100),
        "night_ratio": night_ratio,
        "group_count": len(groups),
    }


def build_prompt(stats: dict, samples: list[str]) -> str:
    """组装推理提示词。"""
    stat_lines = "\n".join(f"- {k}: {v}" for k, v in stats.items())
    sample_block = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(samples))
    return PROMPT.format(stats=stat_lines, samples=sample_block)


# ── 推理 ────────────────────────────────────────────────────────────────


async def infer_persona(
    messages: list[dict],
    call_model,
    cfg: PersonaSettings,
    previous: dict[str, Any] | None = None,
) -> InferenceResult:
    """从消息列表推理用户形象。

    Args:
        messages: :func:`collect_messages` 的产出。
        call_model: 异步 ``(prompt, timeout) -> str``，由调用方注入。
        cfg: 配置。
        previous: 上一次的画像（用于一致性折扣）。

    Returns:
        :class:`InferenceResult`；``ok=False`` 时 ``reason`` 说明原因，
        调用方应保留旧值。
    """
    t0 = time.time()
    result = InferenceResult()

    # 样本门限：宁可不出结论
    if not sufficient(len(messages), cfg.min_samples):
        result.reason = f"样本不足（{len(messages)}/{cfg.min_samples}）"
        result.sample_count = len(messages)
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    stats = compute_stats(messages)
    samples = [
        m["text"][:120] for m in messages[-cfg.sample_turns :] if m.get("text")
    ]
    if not samples:
        result.reason = "没有可用文本"
        result.sample_count = len(messages)
        result.elapsed_ms = int((time.time() - t0) * 1000)
        return result

    prompt = build_prompt(stats, samples)

    try:
        raw = await call_model(prompt, cfg.timeout)
    except Exception as exc:
        result.reason = f"模型调用失败：{type(exc).__name__}: {exc}"
        result.sample_count = len(messages)
        result.elapsed_ms = int((time.time() - t0) * 1000)
        logger.warning(f"[persona] 推理失败：{result.reason}")
        return result

    parsed = parse_llm_result(raw)
    if not parsed or not parsed.get("summary"):
        result.reason = "模型输出无法解析"
        result.sample_count = len(messages)
        result.elapsed_ms = int((time.time() - t0) * 1000)
        logger.warning("[persona] 模型输出无法解析，保留旧画像")
        return result

    conf = effective_confidence(
        len(messages), previous, parsed.get("traits") or {}, cfg.min_samples
    )

    result.ok = True
    result.summary = parsed["summary"]
    result.traits = parsed.get("traits") or {}
    result.evidence = parsed.get("evidence") or []
    result.confidence = conf
    result.sample_count = len(messages)
    result.elapsed_ms = int((time.time() - t0) * 1000)
    return result


# ── 空闲触发判定 ────────────────────────────────────────────────────────


@dataclass
class IdleDecision:
    should_run: bool = False
    reason: str = ""


def should_infer(
    *,
    now: float,
    last_active_at: float | None,
    inferred_times_today: int,
    sample_count: int,
    cfg: PersonaSettings,
    in_flight: bool = False,
) -> IdleDecision:
    """判断现在是否该为该会话做形象推理。

    条件全部满足才推理：已开启、样本够、不在推理中、空闲够久、今日未超次数。
    """
    if not cfg.enabled:
        return IdleDecision(False, "功能未开启")
    if in_flight:
        return IdleDecision(False, "正在推理中")
    if sample_count < cfg.min_samples:
        return IdleDecision(False, f"样本不足（{sample_count}/{cfg.min_samples}）")
    if inferred_times_today >= cfg.max_per_day:
        return IdleDecision(
            False, f"今日次数已满（{inferred_times_today}/{cfg.max_per_day}）"
        )
    if last_active_at is None:
        return IdleDecision(False, "尚无互动记录")
    idle = now - last_active_at
    if idle < cfg.idle_minutes * 60:
        return IdleDecision(False, f"尚未空闲（{int(idle / 60)} 分钟）")
    return IdleDecision(True, f"空闲 {int(idle / 60)} 分钟")
