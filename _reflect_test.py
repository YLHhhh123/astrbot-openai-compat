"""多轮思考单测（不依赖 astrbot 运行时）。

在仓库根目录运行：``python _reflect_test.py``
"""

import asyncio
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "persona_reflect", os.path.join(HERE, "astrbot", "core", "persona", "reflect.py")
)
_mod = importlib.util.module_from_spec(spec)
# dataclass 装饰器会去 sys.modules[cls.__module__] 取模块字典，
# importlib 直载时模块尚未注册 → AttributeError。必须先注册再执行。
sys.modules["persona_reflect"] = _mod
spec.loader.exec_module(_mod)
r = _mod


def check(ok, total, label, cond):
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def cfg(**kw):
    base = {"enabled": True, "rounds": 2, "mode": "progressive", "timeout": 5}
    base.update(kw)
    return r.ReflectionConfig.from_config(base)


def caller(script, fail_on=None):
    """返回一个按script 依次返回内容的 call_model。fail_on=轮次时抛错。"""
    calls = []

    async def call(prompt, timeout):
        i = len(calls)
        calls.append(prompt)
        if fail_on is not None and i == fail_on:
            raise RuntimeError(f"第 {i + 1} 轮模拟失败")
        return script[i] if i < len(script) else script[-1]

    call.calls = calls
    return call


async def main() -> int:
    ok, total = [0], [0]

    # ── 配置解析 ──────────────────────────────────────────────────
    c = cfg()
    check(ok, total, "默认 2 轮", c.rounds == 2)
    check(ok, total, "轮数上限 4", r.ReflectionConfig.from_config({"rounds": 99}).rounds == 4)
    check(ok, total, "轮数下限 1", r.ReflectionConfig.from_config({"rounds": 0}).rounds == 1)
    check(ok, total, "非法轮数回落 2",
          r.ReflectionConfig.from_config({"rounds": "x"}).rounds == 2)
    check(ok, total, "非法间隔回落默认",
          r.ReflectionConfig.from_config({"send_interval": "y"}).send_interval == 2.0)
    check(ok, total, "间隔上限 60",
          r.ReflectionConfig.from_config({"send_interval": 999}).send_interval == 60.0)
    check(ok, total, "mode=off 自动关开关",
          not r.ReflectionConfig.from_config({"mode": "off"}).enabled)
    check(ok, total, "非法 mode 回落 progressive",
          r.ReflectionConfig.from_config({"mode": "weird"}).mode == r.MODE_PROGRESSIVE)
    check(ok, total, "未给 enabled 时默认关",
          not r.ReflectionConfig.from_config({}).enabled)
    check(ok, total, "None 配置容错",
          r.ReflectionConfig.from_config(None).rounds == 2)

    # ── 未启用 / 单轮 ─────────────────────────────────────────────
    out = await r.reflect(caller(["a"]), "hi", r.ReflectionConfig.from_config({}))
    check(ok, total, "未启用直接返回空", not out.segments and out.used_rounds == 0)
    out1 = await r.reflect(caller(["a"]), "hi", cfg(rounds=1))
    check(ok, total, "单轮不触发反思", not out.segments)

    # ── 正常两轮 ──────────────────────────────────────────────────
    c2 = caller(["第一版答复", "改进版答复"])
    out = await r.reflect(c2, "你好", cfg())
    check(ok, total, "跑满 2 轮", out.used_rounds == 2)
    check(ok, total, "最终用改进版", out.final == "改进版答复")
    check(ok, total, "第2轮提示词含第1版草稿", "第一版答复" in c2.calls[1])
    check(ok, total, "第2轮要求自检", "检查" in c2.calls[1] or "改" in c2.calls[1])
    check(ok, total, "渐进模式默认多段", len(out.segments) >= 1)
    check(ok, total, "无降级标记", not out.degraded)

    out_int = await r.reflect(caller(["v1", "v2"]), "hi", cfg(mode="internal"))
    check(ok, total, "internal 模式单段且用改进版",
          len(out_int.segments) == 1 and out_int.segments[0] == "v2")

    out_th = await r.reflect(caller(["v1", "v2"]), "hi", cfg(mode="with_thinking"))
    check(ok, total, "with_thinking 两段", len(out_th.segments) == 2)

    # ── 渐进分段 ──────────────────────────────────────────────────
    out_p = await r.reflect(
        caller(["今天天气不错，适合出门走走。", "对了，记得带伞。"]), "hi", cfg()
    )
    check(ok, total, "渐进模式产出分段", len(out_p.segments) >= 1)
    check(ok, total, "渐进包含最终版", "记得带伞" in "".join(out_p.segments))

    # 改写版是"整段替换"时，不应把两版都发出去
    # 改写版常在复述一小段后继续写（前缀会跨过旧段结尾），必须按重叠度判重
    dup = await r.reflect(
        caller(["我先想想怎么说……", "我先想想怎么说，其实该直接回答你。"]), "hi", cfg()
    )
    check(ok, total, f"重复开头被去重（{len(dup.segments)} 段）", len(dup.segments) == 1)

    # 真正互补的内容应保留为两段
    comp = await r.reflect(
        caller(["今天天气不错适合出门。", "对了，记得带伞。"]), "hi", cfg()
    )
    check(ok, total, f"互补内容保留多段（{len(comp.segments)} 段）", len(comp.segments) >= 2)
    check(ok, total, "两段都在", all(
        seg in "".join(comp.segments)
        for seg in ["今天天气不错适合出门。", "对了，记得带伞。"]
    ))

    # ── 降级（关键：任何失败都要有回复）──────────────────────────
    fail2 = caller(["第一版答复", "x"], fail_on=1)
    out_d = await r.reflect(fail2, "hi", cfg())
    check(ok, total, "第2轮失败不丢弃第1轮", out_d.final == "第一版答复")
    check(ok, total, "标记降级", out_d.degraded)
    check(ok, total, "降级后仍有可发内容", bool(out_d.segments))
    check(ok, total, "只跑了 1 轮", out_d.used_rounds == 1)

    fail1 = caller(["x"], fail_on=0)
    out_d1 = await r.reflect(fail1, "hi", cfg())
    check(ok, total, "第1轮失败 → 无内容（无法降级）", not out_d1.segments)

    empty2 = caller(["第一版", ""], fail_on=None)
    out_e = await r.reflect(empty2, "hi", cfg())
    check(ok, total, "空返回降级到上一轮", out_e.final == "第一版")
    check(ok, total, "空返回标记降级", out_e.degraded)

    # ── 输出清理 ──────────────────────────────────────────────────
    fenced = caller(["```\n有围栏的答复\n```", "改进"])
    out_f = await r.reflect(fenced, "hi", cfg())
    check(ok, total, "去掉代码围栏",
          "```" not in out_f.rounds[0].draft)

    cap = caller(["x" * 200, "y" * 200], fail_on=None)
    out_c = await r.reflect(cap, "hi", cfg(max_chars_per_round=50))
    check(ok, total, "按上限截断", len(out_c.rounds[0].draft) <= 51)

    # ── 分段解析 ──────────────────────────────────────────────────
    check(ok, total, "解析标准 JSON",
          r._parse_split('{"messages":["a","b"]}') == ["a", "b"])
    check(ok, total, "解析带围栏",
          r._parse_split('```json\n{"messages":["a","b"]}\n```') == ["a", "b"])
    check(ok, total, "带解释文字",
          r._parse_split('好的：{"messages":["a","b"]}') == ["a", "b"])
    check(ok, total, "只有一条视为无效", r._parse_split('{"messages":["a"]}') == [])
    check(ok, total, "非 JSON 返回空", r._parse_split("乱码") == [])
    check(ok, total, "None 返回空", r._parse_split(None) == [])
    check(ok, total, "过滤空消息",
          r._parse_split('{"messages":["a","","b"]}') == ["a", "b"])

    split = await r.split_into_messages(caller(['{"messages":["第一段","第二段"]}']), "长文本" * 40, cfg())
    check(ok, total, "拆分成多条", len(split) == 2)
    split_fail = await r.split_into_messages(caller(["乱码"]), "长文本" * 40, cfg())
    check(ok, total, "拆分失败保持原文", split_fail == ["长文本" * 40])
    short = await r.split_into_messages(caller(["x"]), "短", cfg())
    check(ok, total, "短文本不拆分", short == ["短"])

    # ── 耗时记录 ──────────────────────────────────────────────────
    slow = caller(["a", "b"])
    out_s = await r.reflect(slow, "hi", cfg())
    check(ok, total, "记录每轮耗时", all(x.elapsed_ms >= 0 for x in out_s.rounds))

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))