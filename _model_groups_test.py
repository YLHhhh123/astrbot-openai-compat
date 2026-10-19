"""模型分组核心逻辑单测（不依赖 astrbot 运行时）。

在仓库根目录运行：``python _model_groups_test.py``
"""

import asyncio
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))

# 桩掉 astrbot.core.logger
if "astrbot" not in sys.modules:
    _a = types.ModuleType("astrbot"); _a.__path__ = []
    _core = types.ModuleType("astrbot.core"); _core.__path__ = []
    class _L:
        warning = staticmethod(lambda *a, **k: None)
        info = staticmethod(lambda *a, **k: None)
        debug = staticmethod(lambda *a, **k: None)
        error = staticmethod(lambda *a, **k: None)
    _core.logger = _L()
    sys.modules.update({"astrbot": _a, "astrbot.core": _core})

# sp 桩必须在测试轮询之前注入
_store = {}


class _FakeSp:
    @staticmethod
    async def global_get(k, d=None):
        return _store.get(k, d)

    @staticmethod
    async def global_put(k, v):
        _store[k] = v


_api_stub = types.ModuleType("astrbot.api")
_api_stub.sp = _FakeSp
sys.modules["astrbot.api"] = _api_stub
sys.modules["astrbot"].api = _api_stub

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "model_groups", os.path.join(HERE, "astrbot", "core", "provider", "model_groups.py")
)
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)


def check(ok, total, label, cond) -> None:
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def grp(name, members, **kw):
    d = {"name": name, "members": members}
    d.update(kw)
    return d


def main() -> int:
    ok, total = [0], [0]

    # ── 加载与规整 ────────────────────────────────────────────────
    cfg = {"model_groups": [
        grp("daily", ["HomeAPI/a", "HomeAPI/b"]),
        grp("coding", ["HomeAPI/c"], params={"temperature": 0.2}),
        grp("off", ["x"], enabled=False),
        "不是字典",
        {"members": ["缺名字"]},
    ]}
    gs = mg.load_groups(cfg)
    check(ok, total, "加载 3 个有效组", len(gs) == 3)
    check(ok, total, "非法条目被跳过", "不是字典" not in gs)
    check(ok, total, "缺 name 的被跳过", "" not in gs)
    check(ok, total, "禁用组标记正确", gs["off"]["enabled"] is False)
    check(ok, total, "缺策略默认 fallback",
          gs["daily"]["strategy"] == mg.STRATEGY_FALLBACK)
    check(ok, total, "非法策略回落 fallback",
          mg.load_groups({"model_groups": [grp("x", ["a"], strategy="weird")]})["x"]["strategy"]
          == mg.STRATEGY_FALLBACK)
    check(ok, total, "空配置返回空", mg.load_groups({}) == {})
    check(ok, total, "None 配置不崩", mg.load_groups({"model_groups": None}) == {})

    # ── 展开（含嵌套）─────────────────────────────────────────────
    gs2 = mg.load_groups({"model_groups": [
        grp("daily", ["HomeAPI/a", "HomeAPI/b"]),
        grp("coding", ["HomeAPI/c"]),
        grp("work", ["coding", "daily"]),          # 嵌套：组引用组
        grp("deep", ["work", "HomeAPI/z"]),
    ]})
    check(ok, total, "扁平展开", mg.expand_group("daily", gs2) == ["HomeAPI/a", "HomeAPI/b"])
    check(ok, total, "嵌套展开保持顺序",
          mg.expand_group("work", gs2) == ["HomeAPI/c", "HomeAPI/a", "HomeAPI/b"])
    check(ok, total, "多层嵌套",
          mg.expand_group("deep", gs2) == ["HomeAPI/c", "HomeAPI/a", "HomeAPI/b", "HomeAPI/z"])
    check(ok, total, "去重不重复",
          mg.expand_group("dup", mg.load_groups({"model_groups": [
              grp("dup", ["a", "b", "a", "b"])]})) == ["a", "b"])

    # 未注册的裸 provider id 仍可用（迁移兼容）
    check(ok, total, "裸 provider id 原样返回",
          mg.expand_group("HomeAPI/solo", gs2) == ["HomeAPI/solo"])

    # 禁用组展开为空
    gs3 = mg.load_groups({"model_groups": [grp("off", ["a"], enabled=False)]})
    check(ok, total, "禁用组展开为空", mg.expand_group("off", gs3) == [])

    # ── 循环检测 ──────────────────────────────────────────────────
    cyc = mg.load_groups({"model_groups": [grp("a", ["b"]), grp("b", ["a"])]})
    raised = False
    try:
        mg.expand_group("a", cyc)
    except mg.GroupError as exc:
        raised = "a -> b -> a" in str(exc)
    check(ok, total, "循环引用被检测并报出链路", raised)
    selfcyc = mg.load_groups({"model_groups": [grp("s", ["s"])]})
    raised2 = False
    try:
        mg.expand_group("s", selfcyc)
    except mg.GroupError:
        raised2 = True
    check(ok, total, "自引用被检测", raised2)
    # 循环组的 resolve 应优雅降级（返回空 + 组定义）
    ids, g = asyncio.run(mg.resolve_group_providers("a", {"model_groups": cyc["a"].items()}))
    check(ok, total, "循环组 resolve 不抛异常", isinstance(ids, list))

    # ── 厂商识别与参数映射 ────────────────────────────────────────
    check(ok, total, "识别 gemini", mg.detect_vendor("gemini-2.5-pro") == "gemini")
    check(ok, total, "识别 google 源", mg.detect_vendor("x", "google") == "gemini")
    check(ok, total, "识别 claude", mg.detect_vendor("claude-3") == "anthropic")
    check(ok, total, "识别 qwen", mg.detect_vendor("qwen-max") == "qwen")
    check(ok, total, "识别 glm/zhipu", mg.detect_vendor("glm-4.7") == "zhipu")
    check(ok, total, "识别 kimi/moonshot", mg.detect_vendor("kimi-k2") == "moonshot")
    check(ok, total, "未知厂商返回空", mg.detect_vendor("my-custom") == "")

    p = {"temperature": 0.3, "max_output_tokens": 4096}
    check(ok, total, "OpenAI 兼容：max_tokens",
          mg.map_params_to_provider(p, "HomeAPI/x") == {"temperature": 0.3, "max_tokens": 4096})
    check(ok, total, "Gemini：max_output_tokens",
          mg.map_params_to_provider(p, "gemini-2.5-pro")
          == {"temperature": 0.3, "max_output_tokens": 4096})
    check(ok, total, "按 source_id 识别",
          mg.map_params_to_provider(p, "HomeAPI/x", "google")["max_output_tokens"] == 4096)
    check(ok, total, "私有参数原样透传",
          mg.map_params_to_provider({"thinking_budget": 100}, "gemini")
          == {"thinking_budget": 100})
    check(ok, total, "None 值被丢弃",
          mg.map_params_to_provider({"temperature": None}, "x") == {})
    check(ok, total, "空参数返回空", mg.map_params_to_provider({}, "x") == {})
    check(ok, total, "非 dict 容错", mg.map_params_to_provider(None, "x") == {})

    # ── 适用范围 ──────────────────────────────────────────────────
    gs4 = mg.load_groups({"model_groups": [
        grp("g1", ["a"], scope={"umo": ["12345"]}),
        grp("g2", ["b"], scope={"default": True}),
        grp("g3", ["c"], scope={"umo": ["999"]}),
    ]})
    umo = "aiocqhttp:GroupMessage:12345"
    check(ok, total, "群级匹配优先于全局默认",
          mg.find_group_for_scope(gs4, umo) == "g1")
    check(ok, total, "未匹配时用全局默认",
          mg.find_group_for_scope(gs4, "aiocqhttp:GroupMessage:77777") == "g2")
    check(ok, total, "显式 group_id 最高优先",
          mg.find_group_for_scope(gs4, umo, "g3") == "g3")
    check(ok, total, "无 umo 时看全局默认",
          mg.find_group_for_scope(gs4, None) == "g2")
    check(ok, total, "空分组返回 None", mg.find_group_for_scope({}, umo) is None)

    # ── resolve + 轮询 ───────────────────────────────────────────
    cfg_fb = {"model_groups": [grp("d", ["a", "b", "c"])]}
    ids, g = asyncio.run(mg.resolve_group_providers("d", cfg_fb))
    check(ok, total, "fallback 保持原序", ids == ["a", "b", "c"])
    check(ok, total, "fallback 策略正确", g["strategy"] == mg.STRATEGY_FALLBACK)

    ids_none, _ = asyncio.run(mg.resolve_group_providers("不存在", cfg_fb))
    check(ok, total, "不存在的组返回空", ids_none == [])
    ids_empty, _ = asyncio.run(mg.resolve_group_providers("d", {"model_groups": [grp("d", [])]}))
    check(ok, total, "空成员组返回空", ids_empty == [])

    # 轮询（sp 桩已在文件头注入）
    store = _store
    seq = []
    for _ in range(4):
        seq.append(asyncio.run(mg.next_round_robin(["a", "b", "c"], "rr_basic")))
    check(ok, total, "轮询依次推进", seq[:3] == ["a", "b", "c"])
    check(ok, total, "轮询循环回绕", seq[3] == "a")
    check(ok, total, "单成员轮询恒定",
          asyncio.run(mg.next_round_robin(["solo"], "rr_solo")) == "solo")
    check(ok, total, "空列表轮询返回 None",
          asyncio.run(mg.next_round_robin([], "rr")) is None)

    cfg_rr = {"model_groups": [grp("rr_seq", ["a", "b", "c"], strategy="round_robin")]}
    picks = []
    orders = []
    for _ in range(3):
        ids_rr, gg = asyncio.run(mg.resolve_group_providers("rr_seq", cfg_rr))
        picks.append(ids_rr[0])
        orders.append(list(ids_rr))
    # 轮询：每次主模型前移一位，整组成员不变（顺序随之旋转）
    check(ok, total, "resolve 走轮询，主模型依次轮转", picks == ["a", "b", "c"])
    check(ok, total, "轮询后成员集合不变", all(set(o) == {"a", "b", "c"} for o in orders))
    check(ok, total, "轮询后顺序从选中项起旋转",
          orders[1] == ["b", "c", "a"] and orders[2] == ["c", "a", "b"])

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(main())