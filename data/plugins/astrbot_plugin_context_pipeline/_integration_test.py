"""管线调度逻辑集成测试（不依赖 astrbot 运行时）。

在插件目录下运行：``python _integration_test.py``
"""

import asyncio
import gc
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stages import (  # noqa: E402
    STAGE_SPECS,
    PipelineContext,
    StageRegistry,
    resolve_stage_overrides,
    stage_config_key,
)

ORDERED = sorted(STAGE_SPECS, key=lambda s: s.order)


class Recorder:
    """记录自身 key 的假钩子。"""

    def __init__(self, key, sink=None, fail=False):
        self.key = key
        self.sink = sink
        self.fail = fail
        self.calls = 0

    async def __call__(self, event, req):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.key} 故意失败")
        if self.sink is not None:
            self.sink.append(self.key)


class FakePlugin:
    """模拟被委托插件：star_map 持有它（强引用），它导出钩子。"""

    def __init__(self, name, hook, export=True):
        self.name = name
        self._hook = hook
        self._export = export

    def exported_hooks(self):
        return {"on_llm_request": self._hook} if self._export else {}

    def __repr__(self):
        return f"<FakePlugin {self.name}>"


def check(ok, total, label, cond) -> None:
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


async def main() -> int:
    ok, total = [0], [0]

    # ── 1. 阶段表结构 ──────────────────────────────────────────────
    keys = [s.key for s in ORDERED]
    check(ok, total, "压缩阶段唯一且最后", keys[-1] == "prompt_compress" and keys.count("prompt_compress") == 1)
    check(
        ok,
        total,
        "主动回复在自学习之后",
        keys.index("active_reply_prompt") > keys.index("self_learning"),
    )
    orders = [s.order for s in ORDERED]
    check(ok, total, "order 无重复", len(orders) == len(set(orders)))

    # ── 2. 按阶段表顺序执行 ────────────────────────────────────────
    calls: list = []
    keep_alive = []
    reg = StageRegistry()
    for spec in ORDERED:
        inst = FakePlugin(spec.source_plugin, Recorder(spec.key, calls))
        keep_alive.append(inst)  # 模拟 star_map 的强引用
        reg.bind(spec.key, inst)
    for spec in ORDERED:
        await reg.get(spec.key).exported_hooks()["on_llm_request"](None, calls)
    check(ok, total, "实际执行顺序 == 阶段表顺序", calls == keys)

    # ── 3. 单阶段失败被隔离 ────────────────────────────────────────
    done: list = []
    keep2 = []
    reg2 = StageRegistry()
    for spec in ORDERED:
        rec = Recorder(spec.key, done, fail=(spec.key == "worldbook"))
        inst = FakePlugin(spec.source_plugin, rec)
        keep2.append(inst)
        reg2.bind(spec.key, inst)
    failed = []
    for spec in ORDERED:
        try:
            await reg2.get(spec.key).exported_hooks()["on_llm_request"](None, None)
        except Exception:
            failed.append(spec.key)
    check(
        ok,
        total,
        "失败阶段被隔离，其余继续",
        failed == ["worldbook"] and len(done) == len(ORDERED) - 1,
    )

    # ── 4. 开关覆盖 ────────────────────────────────────────────────
    # 平铺键（当前 schema 写法）
    ov = resolve_stage_overrides({"stage_worldbook": False})
    check(ok, total, "平铺键开关生效", ov["worldbook"] is False and ov["group_context"] is True)
    ov_all = resolve_stage_overrides({})
    check(ok, total, "无配置时委托阶段全默认启用",
          all(ov_all[s.key] for s in ORDERED))
    check(ok, total, "无配置时内置阶段保持默认（清理/检查均关）",
          ov_all["context_cleanup"] is False)
    ov3 = resolve_stage_overrides({"stage_不存在的阶段": False})
    check(ok, total, "未知平铺键被忽略", "不存在的阶段" not in ov3)
    check(ok, total, "委托+内置阶段都在返回值里",
          len(ov3) == len(ORDERED) + 2)
    #旧嵌套写法仍兼容
    ov4 = resolve_stage_overrides({"stages": {"group_context": False}})
    check(ok, total, "旧嵌套写法向后兼容", ov4["group_context"] is False)
    # 平铺键优先于嵌套
    ov5 = resolve_stage_overrides({"stage_worldbook": True, "stages": {"worldbook": False}})
    check(ok, total, "平铺键优先于嵌套", ov5["worldbook"] is True)
    # 键名映射
    check(ok, total, "键名映射为 stage_<key>", stage_config_key("worldbook") == "stage_worldbook")
    # 内置阶段也必须能被解析（曾漏掉导致清理上下文永远关闭）
    ovc = resolve_stage_overrides({"stage_context_cleanup": True})
    check(ok, total, "内置阶段可被启用", ovc.get("context_cleanup") is True)
    check(ok, total, "内置阶段默认关闭", resolve_stage_overrides({}).get("context_cleanup") is False)
    check(ok, total, "内置阶段键也在返回值里", "output_check" in resolve_stage_overrides({}))

    # ── 5. 弱引用生命周期 ──────────────────────────────────────────
    keep3 = []
    reg3 = StageRegistry()
    t = FakePlugin("tmp", Recorder("tmp"))
    keep3.append(t)
    reg3.bind("x", t)
    check(ok, total, "绑定后可取回实例", reg3.get("x") is t)
    check(ok, total, "存活时 stale_keys 为空", reg3.stale_keys() == [])
    keep3.clear()
    del t
    gc.collect()
    check(ok, total, "实例被回收后 get() 返回 None", reg3.get("x") is None)
    check(ok, total, "回收后列入 stale_keys（供自愈重绑）", reg3.stale_keys() == ["x"])
    check(ok, total, "bound_keys 不含失效项", "x" not in reg3.bound_keys())

    # ── 6. 未导出钩子时的回退判定 ────────────────────────────────
    no_export = FakePlugin("ne", Recorder("ne"), export=False)
    check(
        ok,
        total,
        "未导出钩子时该阶段视为不可用",
        "on_llm_request" not in (no_export.exported_hooks() or {}),
    )

    # ── 7. PipelineContext 记录 ────────────────────────────────────
    ctx = PipelineContext()
    ctx.mark("worldbook", ran=True, elapsed_ms=12.34567)
    ctx.mark("group_context", skipped="阶段开关关闭")
    d = ctx.states
    check(ok, total, "上下文记录 ran", d["worldbook"].ran is True)
    check(ok, total, "上下文记录 skipped", d["group_context"].skipped == "阶段开关关闭")
    check(ok, total, "耗时已四舍五入", d["worldbook"].to_dict()["elapsed_ms"] == 12.35)

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
