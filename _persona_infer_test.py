"""形象推理层单测（不依赖 astrbot 运行时）。

在仓库根目录运行：``python _persona_infer_test.py``
"""

import asyncio
import importlib
import importlib.util
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass 需要模块已注册
    spec.loader.exec_module(mod)
    return mod


m = load("persona_models", "astrbot/core/persona/models.py")

# infer.py 用的是 `from .models import ...`，必须让它处在真实包上下文里，
# 因此要把 astrbot / astrbot.core / persona 三个包的 __path__ 都指向磁盘。
# 注意 models 已经以 "persona_models" 注册过一次；这里用同一份模块对象，
# 否则 SQLModel 会重复注册同名表并抛 InvalidRequestError。
if "astrbot" not in sys.modules:
    _a = types.ModuleType("astrbot")
    _a.__path__ = [os.path.join(HERE, "astrbot")]
    _core = types.ModuleType("astrbot.core")
    _core.__path__ = [os.path.join(HERE, "astrbot", "core")]
    class _L:
        warning = staticmethod(lambda *x, **k: None)
        info = staticmethod(lambda *x, **k: None)
        debug = staticmethod(lambda *x, **k: None)
    _core.logger = _L()
    _pkg = types.ModuleType("astrbot.core.persona")
    _pkg.__path__ = [os.path.join(HERE, "astrbot", "core", "persona")]
    sys.modules.update({
        "astrbot": _a,
        "astrbot.core": _core,
        "astrbot.core.persona": _pkg,
    })
    # 让 `from .models import ...` 命中已加载的同一份，避免二次注册 ORM 表
    sys.modules["astrbot.core.persona.models"] = m
    _pkg.models = m

inf = importlib.import_module("astrbot.core.persona.infer")


def check(ok, total, label, cond):
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def msgs(n, text="在吗"):
    return [{"text": text, "sender_id": "u1", "user_id": "g1", "at": 1700000000.0}
            for _ in range(n)]


def cfg(**kw):
    base = {"enabled": True, "min_samples": 20, "max_per_day": 3, "idle_minutes": 10}
    base.update(kw)
    return inf.PersonaSettings.from_config(base)


def model_returning(value, fail=False):
    calls = []

    async def call(prompt, timeout):
        calls.append(prompt)
        if fail:
            raise RuntimeError("模拟模型不可用")
        return value

    call.calls = calls
    return call


async def main() -> int:
    ok, total = [0], [0]

    # ── 消息文本提取 ──────────────────────────────────────────────
    check(ok, total, "提取 str", inf._extract_text("你好") == "你好")
    check(ok, total, "提取 dict.plain", inf._extract_text({"plain": "a"}) == "a")
    check(ok, total, "提取 dict.text", inf._extract_text({"text": "b"}) == "b")
    check(ok, total, "提取消息链首段",
          inf._extract_text({"chain": [{"type": "text", "text": "链上文字"}]}) == "链上文字")
    check(ok, total, "提取 list 里的文本",
          inf._extract_text([{"text": "x"}, {"text": "y"}]) == "x y")
    check(ok, total, "空内容", inf._extract_text(None) == "")
    check(ok, total, "无法识别结构", inf._extract_text({"foo": 1}) == "")

    class Row:
        def __init__(self, c, sid="u1", nm="某用户"):
            self.content = c
            self.sender_id = sid
            self.sender_name = nm
            self.user_id = "g1"
            self.platform_id = "aiocqhttp"
            self.created_at = 1700000000.0

    collected = inf.collect_messages([Row({"plain": f"消息{i}"}) for i in range(5)])
    check(ok, total, "collect 取 5 条", len(collected) == 5)
    check(ok, total, "collect 带用户名", collected[0]["sender_name"] == "某用户")
    check(ok, total, "collect 取尾部 limit",
          len(inf.collect_messages([Row({"plain": str(i)}) for i in range(50)], limit=10)) == 10)
    check(ok, total, "collect 跳过无文本", len(inf.collect_messages([Row({})])) == 0)

    # ── 客观统计 ──────────────────────────────────────────────────
    st = inf.compute_stats([{"text": "在吗？", "at": 1700000000.0}] * 5)
    check(ok, total, "统计总数", st["total"] == 5)
    check(ok, total, "平均句长", st["avg_len"] == 3)
    check(ok, total, "提问占比 100%", st["question_ratio"] == 100)
    check(ok, total, "空列表不崩", inf.compute_stats([]) == {"total": 0})

    st2 = inf.compute_stats([{"text": "哈哈哈", "at": 1700000000.0}] * 4)
    check(ok, total, "笑声占比", st2["laugh_ratio"] == 100)
    st3 = inf.compute_stats([{"text": "嗯", "at": 1700000000.0}] * 3)
    check(ok, total, "短句占比", st3["short_ratio"] == 100)

    # ── 配置解析 ──────────────────────────────────────────────────
    check(ok, total, "默认最小样本 20", inf.PersonaSettings.from_config({}).min_samples == 20)
    check(ok, total, "空闲上限 1440",
          inf.PersonaSettings.from_config({"idle_minutes": 99999}).idle_minutes == 1440)
    check(ok, total, "每日次数下限 1",
          inf.PersonaSettings.from_config({"max_per_day": 0}).max_per_day == 1)
    check(ok, total, "非法值回落",
          inf.PersonaSettings.from_config({"idle_minutes": "x"}).idle_minutes == 10)
    check(ok, total, "None 配置容错", inf.PersonaSettings.from_config(None).enabled is False)

    # ── 推理：样本门限 ────────────────────────────────────────────
    r = await inf.infer_persona(msgs(5), model_returning("{}"), cfg())
    check(ok, total, "样本不足不出结论", not r.ok and "样本不足" in r.reason)

    good_json = ('{"summary":"爱接梗的活跃分子","traits":{"性格":["乐观"]},'
                 '"evidence":["他总在群里接话"]}')

    # ── 推理：正常路径 ────────────────────────────────────────────
    call = model_returning(good_json)
    r = await inf.infer_persona(msgs(30), call, cfg())
    check(ok, total, "推理成功", r.ok)
    check(ok, total, "取到 summary", r.summary == "爱接梗的活跃分子")
    check(ok, total, "取到 evidence", len(r.evidence) == 1)
    check(ok, total, "置信度 0.6（30 样本）", r.confidence == 0.6)
    check(ok, total, "提示词含统计", "客观统计" in call.calls[0])
    check(ok, total, "提示词含摘录", "在吗" in call.calls[0])

    r400 = await inf.infer_persona(msgs(400), model_returning(good_json), cfg())
    check(ok, total, "大样本置信度 0.95", r400.confidence == 0.95)

    # 一致性折扣
    prev = {"traits": {"性格": ["内向"], "习惯": ["独处"]}}
    r_dec = await inf.infer_persona(
        msgs(400), model_returning('{"summary":"x","traits":{"性格":["外向"]}}'), cfg(),
        previous=prev,
    )
    check(ok, total, f"矛盾结论降置信度（{r_dec.confidence}）", r_dec.confidence < 0.95)

    # ── 推理：失败不产出（调用方会保留旧值）──────────────────────
    r_fail = await inf.infer_persona(msgs(30), model_returning("", fail=True), cfg())
    check(ok, total, "模型异常 → 不产出", not r_fail.ok and "模型调用失败" in r_fail.reason)
    r_junk = await inf.infer_persona(msgs(30), model_returning("这不是 JSON"), cfg())
    check(ok, total, "输出不可解析 → 不产出", not r_junk.ok)
    r_empty = await inf.infer_persona(msgs(30), model_returning('{"summary":""}'), cfg())
    check(ok, total, "空 summary → 不产出", not r_empty.ok)
    r_noText = await inf.infer_persona(
        [{"text": "", "at": 1.0}] * 30, model_returning(good_json), cfg()
    )
    check(ok, total, "无可用文本 → 不产出", not r_noText.ok)

    # ── 空闲触发判定 ─────────────────────────────────────────────
    now = 1700000000.0
    ok_case = inf.should_infer(
        now=now, last_active_at=now - 20 * 60, inferred_times_today=0,
        sample_count=50, cfg=cfg(),
    )
    check(ok, total, f"空闲够久应推理（{ok_case.reason}）", ok_case.should_run)

    check(ok, total, "未开启不推理",
          not inf.should_infer(now=now, last_active_at=now - 999, inferred_times_today=0,
                               sample_count=50, cfg=cfg(enabled=False)).should_run)
    check(ok, total, "样本不足不推理",
          not inf.should_infer(now=now, last_active_at=now - 999, inferred_times_today=0,
                               sample_count=5, cfg=cfg()).should_run)
    check(ok, total, "今日超次不推理",
          not inf.should_infer(now=now, last_active_at=now - 999, inferred_times_today=3,
                               sample_count=50, cfg=cfg()).should_run)
    check(ok, total, "正在推理不重复",
          not inf.should_infer(now=now, last_active_at=now - 999, inferred_times_today=0,
                               sample_count=50, cfg=cfg(), in_flight=True).should_run)
    check(ok, total, "刚说过话不推理",
          not inf.should_infer(now=now, last_active_at=now - 60, inferred_times_today=0,
                               sample_count=50, cfg=cfg()).should_run)
    check(ok, total, "无互动记录不推理",
          not inf.should_infer(now=now, last_active_at=None, inferred_times_today=0,
                               sample_count=50, cfg=cfg()).should_run)

    # 边界：刚好等于阈值
    edge = inf.should_infer(now=now, last_active_at=now - 10 * 60,
                            inferred_times_today=0, sample_count=20, cfg=cfg())
    check(ok, total, "边界：恰好等于阈值应推理", edge.should_run)
    edge2 = inf.should_infer(now=now, last_active_at=now - 10 * 60 + 1,
                             inferred_times_today=0, sample_count=20, cfg=cfg())
    check(ok, total, "边界：差1 秒不推理", not edge2.should_run)

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))