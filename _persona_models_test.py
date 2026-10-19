"""用户形象数据层单测（不依赖 astrbot 运行时）。

在仓库根目录运行：``python _persona_models_test.py``
"""

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "persona_models", os.path.join(HERE, "astrbot", "core", "persona", "models.py")
)
_mod = importlib.util.module_from_spec(spec)
# dataclass 装饰器会去 sys.modules[cls.__module__] 取模块字典，
# importlib 直载时模块尚未注册 → AttributeError。必须先注册再执行。
sys.modules["persona_models"] = _mod
spec.loader.exec_module(_mod)
m = _mod


def check(ok, total, label, cond):
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def main() -> int:
    ok, total = [0], [0]

    # ── 置信度分档 ────────────────────────────────────────────────
    check(ok, total, "0 样本 → 0", m.confidence_for(0) == 0.0)
    check(ok, total, "10 样本 → 仍0", m.confidence_for(10) == 0.0)
    check(ok, total, "20 样本 → 0.6", m.confidence_for(20) == 0.6)
    check(ok, total, "99 样本 → 0.6", m.confidence_for(99) == 0.6)
    check(ok, total, "100 样本 → 0.8", m.confidence_for(100) == 0.8)
    check(ok, total, "299 样本 → 0.8", m.confidence_for(299) == 0.8)
    check(ok, total, "300 样本 → 0.95", m.confidence_for(300) == 0.95)
    check(ok, total, "99999 样本不超上限", m.confidence_for(99999) == m.MAX_CONFIDENCE)
    check(ok, total, "负数样本容错", m.confidence_for(-5) == 0.0)
    check(ok, total, "浮点输入容错", m.confidence_for(50.7) == 0.6)

    # ── 样本门限 ──────────────────────────────────────────────────
    check(ok, total, "19 条不够", not m.sufficient(19))
    check(ok, total, "20 条够", m.sufficient(20))
    check(ok, total, "自定义门限生效", m.sufficient(10, 10) and not m.sufficient(9, 10))

    # ── 一致性折扣 ────────────────────────────────────────────────
    same = {"性格": ["外向"], "习惯": ["熬夜"]}
    check(ok, total, "首次推理不打折", m.decay_by_inconsistency(None, same) == 1.0)
    check(ok, total, "结论一致不打折",
          m.decay_by_inconsistency({"traits": same}, same) == 1.0)
    opposed = m.decay_by_inconsistency(
        {"traits": {"性格": ["内向", "谨慎"], "习惯": ["独处"]}},
        {"traits": {"性格": ["外向", "冲动"], "习惯": ["社交"]}},
    )
    check(ok, total, f"结论相反要打折（{opposed:.2f} < 1.0）", 0.5 <= opposed < 1.0)
    # 完全不同的维度 → 应大幅打折
    few = m.decay_by_inconsistency({"traits": {"性格": ["内向"]}}, {"性格": ["外向"]})
    check(ok, total, f"完全矛盾要打折（{few} < 1.0）", few < 1.0)
    check(ok, total, "折扣不低于 0.5", few >= 0.5)
    check(ok, total, "折扣不低于 0.5", opposed >= 0.5)

    # ── 最终置信度 ────────────────────────────────────────────────
    check(ok, total, "样本不足 → 0", m.effective_confidence(5, None, same) == 0.0)
    check(ok, total, "样本充足 → 0.6", m.effective_confidence(50, None, same) == 0.6)
    # previous 传「上一次的形象本体」，其traits 键即旧结论
    prev_persona = {"traits": {"性格": ["外向"], "习惯": ["熬夜"]}}
    e = m.effective_confidence(400, prev_persona, {"性格": ["外向"], "习惯": ["熬夜"]})
    check(ok, total, "大样本一致 → 0.95", e == 0.95)
    e2 = m.effective_confidence(
        400, prev_persona, {"性格": ["内向"], "习惯": ["独处"]}
    )
    check(ok, total, f"大样本但矛盾 → 打折（{e2}）", e2 < 0.95)

    # ── LLM 输出解析 ──────────────────────────────────────────────
    clean = '{"summary":"乐子人","traits":{"性格":["爱抬杠"]},"evidence":["他总在群里吐槽"]}'
    r = m.parse_llm_result(clean)
    check(ok, total, "干净 JSON 解析", r["summary"] == "乐子人")
    check(ok, total, "traits 成list", r["traits"]["性格"] == ["爱抬杆"] or r["traits"]["性格"] == ["爱抬杠"])

    fenced = '```json\n{"summary":"A","traits":{"性格":["B"]}}\n```'
    check(ok, total, "代码块包裹", m.parse_llm_result(fenced)["summary"] == "A")
    prefixed = '好的，这是分析结果：\n{"summary":"C","traits":{}}'
    check(ok, total, "前后有解释文字", m.parse_llm_result(prefixed)["summary"] == "C")

    # 中文字段名
    cn = '{"画像":"内向人","特征":{"性格":["安静"]},"证据":["很少发言"]}'
    r2 = m.parse_llm_result(cn)
    check(ok, total, "中文字段名", r2["summary"] == "内向人" and "性格" in r2["traits"])
    check(ok, total, "中文字段→证据", r2["evidence"][0]["text"] == "很少发言")

    # 容错
    check(ok, total, "空输入", m.parse_llm_result("") == {})
    check(ok, total, "None 输入", m.parse_llm_result(None) == {})
    check(ok, total, "非 JSON", m.parse_llm_result("完全不是JSON") == {})
    check(ok, total, "JSON 数组而非对象", m.parse_llm_result("[1,2,3]") == {})

    # 字段规整
    odd = '{"summary":"x","traits":["直接是数组"],"evidence":["纯字符串"]}'
    r3 = m.parse_llm_result(odd)
    check(ok, total, "traits 是数组时归入综合", "综合" in r3["traits"])
    check(ok, total, "字符串证据归一为 dict", isinstance(r3["evidence"][0], dict))
    ev_dict = '{"summary":"x","evidence":[{"text":"t","sender":"s"}]}'
    r4 = m.parse_llm_result(ev_dict)
    check(ok, total, "dict 证据保留 sender", r4["evidence"][0]["sender"] == "s")
    check(ok, total, "过多证据被截断到 6",
          len(m.parse_llm_result(json_dumps_evidence(20))["evidence"]) <= 6)

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


def json_dumps_evidence(n):
    import json
    return json.dumps({"summary": "x", "evidence": [f"e{i}" for i in range(n)]})


if __name__ == "__main__":
    sys.exit(main())