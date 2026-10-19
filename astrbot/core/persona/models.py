"""用户形象的数据模型。

一个用户**一份全局形象**，外加**每个群各自的修正项**（同一个人在不同群表现
可能完全不同：上班群正经、群里玩梗）。两者分开存而不是合成一条记录，是为了让
全局形象不被单一群的样本带偏。

样本量与置信度
--------------
形象是从聊天记录里推出来的，3 条消息不足以支撑任何结论。因此：

* 样本量不足 ``min_samples`` → **不出结论**，画像留空；
* 样本越多置信度越高，但有上限 —— 再多也不会到1.0，因为画像本质是概括而非事实；
* 同一用户多次推理的结论若自相矛盾，置信度打折（见 :func:`decay_by_inconsistency`）。

这些规则集中在 :func:`confidence_for` 与 :func:`effective_confidence`，改口径时
只动这两处，不要散落到调用点。
"""

from __future__ import annotations

import json
import time
from typing import Any

from sqlmodel import JSON, Field, SQLModel

# 置信度分档：(样本下限, 置信度上限)
CONFIDENCE_TIERS: tuple[tuple[int, float], ...] = (
    (0, 0.0),      # 无样本
    (20, 0.6),     # 初步
    (100, 0.8),
    (300, 0.95),
)
DEFAULT_MIN_SAMPLES = 20
MAX_CONFIDENCE = 0.95


class UserPersona(SQLModel, table=True):
    """单个用户的全局形象。"""

    __tablename__ = "user_personas"

    user_id: str = Field(primary_key=True, max_length=255)
    # 一句话形象，例如「爱抬杠的乐子人，偏夜猫子」
    summary: str = Field(default="", max_length=1000)
    # 性格/习惯/情绪/关系 四类标签
    traits: dict | None = Field(default=None, sa_type=JSON)
    # 证据：[{text, sender, at, source}]，供面板溯源，避免"凭空评价"
    evidence: list | None = Field(default=None, sa_type=JSON)
    # 0~1
    confidence: float = Field(default=0.0)
    sample_count: int = Field(default=0)
    # 推理时用的模型（溯源用；可能是分组名）
    persona_model_id: str = Field(default="", max_length=255)
    updated_at: float = Field(default_factory=time.time)

    @property
    def sufficient(self) -> bool:
        """样本是否够下结论。"""
        return self.sample_count >= DEFAULT_MIN_SAMPLES

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "summary": self.summary,
            "traits": self.traits or {},
            "evidence": self.evidence or [],
            "confidence": round(self.confidence, 3),
            "sample_count": self.sample_count,
            "persona_model_id": self.persona_model_id,
            "updated_at": self.updated_at,
        }


class PersonaGroupAdjust(SQLModel, table=True):
    """用户在某个群里的修正项。

    与全局形象分开：同一个人在 A 群可能是话痨、在 B 群很沉默。若把群内表现直接
    写进全局形象，会被样本量最大的那个群绑架。
    """

    __tablename__ = "persona_group_adjusts"

    user_id: str = Field(primary_key=True, max_length=255)
    group_id: str = Field(primary_key=True, max_length=255)
    note: str = Field(default="", max_length=1000)
    traits: dict | None = Field(default=None, sa_type=JSON)
    sample_count: int = Field(default=0)
    updated_at: float = Field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "group_id": self.group_id,
            "note": self.note,
            "traits": self.traits or {},
            "sample_count": self.sample_count,
            "updated_at": self.updated_at,
        }


class PersonaInferenceLog(SQLModel, table=True):
    """推理流水。用来回答"上次为什么没更新"，也便于面板排查。"""

    __tablename__ = "persona_inference_logs"

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(index=True, max_length=255)
    trigger: str = Field(default="idle", max_length=32)
    ok: bool = Field(default=False)
    sample_count: int = Field(default=0)
    reason: str = Field(default="", max_length=500)
    elapsed_ms: int = Field(default=0)
    created_at: float = Field(default_factory=time.time)


# ── 置信度规则 ──────────────────────────────────────────────────────────


def confidence_for(sample_count: int) -> float:
    """按样本量给出置信度上限。"""
    n = max(0, int(sample_count))
    cap = 0.0
    for threshold, value in CONFIDENCE_TIERS:
        if n >= threshold:
            cap = value
    return min(cap, MAX_CONFIDENCE)


def sufficient(sample_count: int, min_samples: int = DEFAULT_MIN_SAMPLES) -> bool:
    """样本是否够下结论。"""
    return int(sample_count) >= int(min_samples)


def decay_by_inconsistency(
    previous: dict[str, Any] | None, new_traits: dict[str, Any]
) -> float:
    """结论自相矛盾时的折扣系数，返回 0.5~1.0。

    判断口径是**标签类别的重合度**，不是标签文字的重合度：上一次说「内向」、
    这一次说「外向」，文字零交集，但它们说的是同一类特质、结论相反 ——
    这才是最该打折的情况。若按文字求交集，这种矛盾反而会被判成"完全不同的
    两个人"而放过。

    所以这里比的是"有哪些特质维度/ 出现了多少标签"，重合越低说明两次结论
    差异越大，可信度折扣越重。
    """
    if not previous:
        return 1.0
    old = previous.get("traits") or {}
    if not isinstance(old, dict) or not isinstance(new_traits, dict):
        return 1.0

    old_keys, old_items = _flatten(old)
    new_keys, new_items = _flatten(new_traits)
    if not old_items or not new_items:
        return 1.0

    # 完全相同的结论不打折
    if old_items == new_items:
        return 1.0

    # 维度重合 + 标签重合，综合成 0~1 的稳定度
    key_union = old_keys | new_keys
    item_union = old_items | new_items
    if not key_union or not item_union:
        return 1.0
    key_score = len(old_keys & new_keys) / len(key_union)
    item_score = len(old_items & new_items) / len(item_union)
    stability = (key_score + item_score) / 2

    return max(0.5, min(1.0, 0.5 + stability * 0.5))


def effective_confidence(
    sample_count: int,
    previous: dict[str, Any] | None = None,
    new_traits: dict[str, Any] | None = None,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> float:
    """最终置信度 = 样本上限 × 一致性折扣。

    样本不足时返回 0，让调用方选择"不更新"或"更新但标注样本不足"。
    """
    if not sufficient(sample_count, min_samples):
        return 0.0
    cap = confidence_for(sample_count)
    decay = decay_by_inconsistency(previous, new_traits or {})
    return round(cap * decay, 3)


def _flatten(traits: dict[str, Any]) -> tuple[set[str], set[str]]:
    """返回 ``(维度集合, 标签集合)``。

    两个维度分开返回，是为了��便上面按"维度重合"和"标签重合"分别打分。
    """
    keys: set[str] = set()
    items: set[str] = set()
    for key, value in traits.items():
        if not isinstance(value, list):
            keys.add(str(key))
            if value not in (None, "", {}):
                items.add(f"{key}:{value}")
            continue
        for item in value:
            if item in (None, ""):
                continue
            keys.add(str(key))
            items.add(f"{key}:{item}")
    return keys, items


def parse_llm_result(raw: str) -> dict[str, Any]:
    """解析推理模型的 JSON 输出。

    模型返回不总是干净 JSON（可能带```json 包裹或前后解释），所以做三层兜底：
    直接 parse → 剥代码块 → 提取最外层花括号。失败返回空 dict，由调用方决定
    是否沿用旧值。
    """
    if not raw or not raw.strip():
        return {}
    text = raw.strip()

    # 剥 ```json ... ```
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
        text = text.split("```")[0].strip()

    for candidate in (text, _extract_braces(text)):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return _normalize_result(data)
    return {}


def _extract_braces(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return None


def _normalize_result(data: dict[str, Any]) -> dict[str, Any]:
    """规整字段，容忍模型自造的名字。"""
    summary = data.get("summary") or data.get("画像") or data.get("描述") or ""

    traits_raw = data.get("traits") or data.get("特征") or {}
    traits: dict[str, list[str]] = {}
    if isinstance(traits_raw, dict):
        for key, value in traits_raw.items():
            if isinstance(value, list):
                traits[str(key)] = [str(v) for v in value if str(v).strip()][:8]
            elif value not in (None, ""):
                traits[str(key)] = [str(value)]
    elif isinstance(traits_raw, list):
        traits["综合"] = [str(v) for v in traits_raw if str(v).strip()][:8]

    evidence_raw = data.get("evidence") or data.get("证据") or []
    evidence: list[dict[str, Any]] = []
    if isinstance(evidence_raw, list):
        for item in evidence_raw[:6]:
            if isinstance(item, str) and item.strip():
                evidence.append({"text": item.strip()[:200]})
            elif isinstance(item, dict):
                evidence.append(
                    {
                        "text": str(item.get("text") or item.get("描述") or "")[:200],
                        "sender": str(item.get("sender") or "")[:64],
                        "at": item.get("at"),
                        "source": str(item.get("source") or "")[:32],
                    }
                )

    return {"summary": str(summary)[:1000], "traits": traits, "evidence": evidence}