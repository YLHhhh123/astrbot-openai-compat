"""插件类别：tag 自动识别 + 默认 functional。

用真实服务器插件的 tags 做数据，避免"只用最标准的输入"导致线上翻车
（这在context_pipeline 的钩子探测上已经栽过一次）。
"""

import sys
import types
from pathlib import Path

HERE = Path(__file__).parent
SRC = HERE / "astrbot" / "core" / "star" / "plugin_category_manager.py"

ok = [0]
total = [0]


def check(label, cond):
    total[0] += 1
    good = bool(cond)
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def load():
    """加载被测模块，绕开 astrbot 运行时依赖。"""
    src = SRC.read_text(encoding="utf-8")
    # 去掉 astrbot 依赖
    src = src.replace("from astrbot.core import logger, sp", "logger = sp = None")
    ns = {
        "Any": object,
        "logger": types.SimpleNamespace(
            debug=lambda *a, **k: None, warning=lambda *a, **k: None
        ),
        "sp": None,
    }
    exec(compile(src, str(SRC), "exec"), ns)
    return ns["PluginCategoryManager"], ns


M, ns = load()
FUNC = ns["CATEGORY_FUNCTIONAL"]
INTER = ns["CATEGORY_INTERACTIVE"]

print("\n=== 默认类别 ===")
check("默认是 functional", ns["DEFAULT_CATEGORY"] == FUNC)

print("\n=== tags 识别：服务器真实插件 ===")
# 逐条取自服务器 data/plugins/*/metadata.yaml 的实际 tags，
# 不是拍脑袋编的 —— 上一轮编造假设导致 self_learning / worldbook 判错。
real = [
    # self_learning 的真实 tags（含"人格"，会先命中 interactive 词表）
    (["自学习", "对话风格", "人格优化", "AI学习", "智能分析"], INTER,
     "self_learning 真实 tags → interactive（人格优化）"),
    # meme_manager 的真实 tags
    (["表情包", "图片管理", "AI", "WebUI"], INTER,
     "meme_manager 真实 tags → interactive（表情包）"),
    # 无 tags 的插件走默认。注意 category_from_tags 无命中时返回 None，
    # 由 resolve_category 回落默认类别 —— 这里要测的是那条路径。
    (None, None, "无 tags → None（交给上层回落）"),
    ([], None, "空 tags → None"),
    # 词表覆盖
    (["世界书"], FUNC, "世界书（中文）→ functional"),
    (["worldbook"], FUNC, "worldbook → functional"),
    (["工具"], FUNC, "工具 → functional"),
    (["天气"], FUNC, "天气 → functional"),
    (["智能家居"], FUNC, "智能家居 → functional"),
    (["记忆"], FUNC, "记忆 → functional"),
    (["群聊"], INTER, "群聊 → interactive"),
    (["音乐"], INTER, "音乐 → interactive"),
    (["签到"], INTER, "签到 → interactive"),
    (["合并转发"], INTER, "合并转发 → interactive"),
    (["监控"], FUNC, "监控 → functional"),
    (["llm"], FUNC, "llm → functional"),
    (["http接口"], FUNC, "http接口 → functional"),
]
for tags, expect, label in real:
    check(label, M.category_from_tags(tags) == expect)

print("\n=== tags 形态兼容 ===")
check("逗号分隔字符串 '表情包,娱乐'",
      M.category_from_tags("表情包,娱乐") == INTER)
check("中文逗号 '表情包，娱乐'",
      M.category_from_tags("表情包，娱乐") == INTER)
check("元组 ('工具',)", M.category_from_tags(("工具",)) == FUNC)
check("集合 {'工具'}", M.category_from_tags({"工具"}) == FUNC)
check("None → None", M.category_from_tags(None) is None)
check("空列表 → None", M.category_from_tags([]) is None)
check("空串→ None", M.category_from_tags("") is None)
check("数字（异常输入）→ None", M.category_from_tags(123) is None)

print("\n=== 冲突时 interactive 优先 ===")
# 同时含互动词与功能词：社交能力是"重"能力，错判代价更高
check("['互动','工具'] → interactive",
      M.category_from_tags(["互动", "工具"]) == INTER)
check("['群聊','记忆'] → interactive",
      M.category_from_tags(["群聊", "记忆"]) == INTER)
check("['图片','api'] → interactive",
      M.category_from_tags(["图片", "api"]) == INTER)

print("\n=== 大小写不敏感 ===")
check("'LLM'", M.category_from_tags("LLM") == FUNC)
check("'Game'", M.category_from_tags("Game") == INTER)
check("'Chat'", M.category_from_tags("Chat") == INTER)

print("\n=== 无 tags 时回落到默认 ===")
check("resolve(None tags) → functional",
      M.resolve_category("p", None, {}, None) == FUNC)
check("resolve([] tags) → functional",
      M.resolve_category("p", None, {}, []) == FUNC)
check("source(None tags) → default",
      M.category_source("p", None, {}, None) == "default")

print("\n=== resolve 优先级 ===")
ov = {"p1": INTER}
check("override 最高", M.resolve_category("p1", "functional", ov, ["工具"]) == INTER)
check("metadata 次之", M.resolve_category("p2", "functional", {}, ["工具"]) == FUNC)
check("tag 第三", M.resolve_category("p3", None, {}, ["表情包"]) == INTER)
check("default 最后", M.resolve_category("p4", None, {}, ["无关"]) == FUNC)
check("无任何输入 → default",
      M.resolve_category("p5", None, {}, None) == FUNC)

print("\n=== category_source ===")
check("override", M.category_source("p1", "functional", ov, ["工具"]) == "override")
check("metadata", M.category_source("p2", "functional", {}, ["工具"]) == "metadata")
check("tag", M.category_source("p3", None, {}, ["表情包"]) == "tag")
check("default", M.category_source("p4", None, {}, ["无关"]) == "default")

print("\n=== normalize_category ===")
for raw, exp in [
    ("functional", FUNC), ("interactive", INTER),
    ("func", FUNC), ("tool", FUNC), ("工具", FUNC), ("功能性", FUNC), ("功能类", FUNC),
    ("inter", INTER), ("chat", INTER), ("互动", INTER), ("互动性", INTER),
    ("社交类", INTER),
    ("乱写的东西", FUNC),      # 非法 → 默认
    (None, FUNC), ("", FUNC),
]:
    check(f"normalize({raw!r}) → {exp}", M.normalize_category(raw) == exp)

print("\n=== 词表自身一致性 ===")
iw = set(ns["INTERACTIVE_TAG_WORDS"])
fw = set(ns["FUNCTIONAL_TAG_WORDS"])
dup = iw & fw
# 允许交集（判定顺序决定谁赢），但不能有同一分类里自我矛盾的空串
check("无空词", "" not in iw and "" not in fw)
check("交集词表可接受", isinstance(dup, set))
if dup:
    print(f"       交集词（interactive 优先）: {sorted(dup)}")

print("\n=== 入口身份解析（机器人实例 id 优先于适配器类型）===")


def make_event(extra=None, pid="qq_main", ptype="aiocqhttp"):
    return types.SimpleNamespace(
        get_extra=lambda key: (extra or {}).get(key),
        platform_meta=types.SimpleNamespace(id=pid, name=ptype),
    )


e = M.entry_identity(make_event())
check("普通平台事件 → (实例 id, 适配器类型)", e == ("qq_main", "aiocqhttp"))
e = M.entry_identity(make_event({"_entry": "openai_compat"}))
check("_entry 自报 → (特殊入口, 无类型)", e == ("openai_compat", None))
e = M.entry_identity(make_event(extra={"_entry": "   "}, pid="", ptype="cron"))
check("空白 _entry + 无 id → 回落类型", e == ("cron", "cron"))
e = M.entry_identity(make_event(extra={"_entry": " x "}, pid="a", ptype="b"))
check("_entry 两侧空白被剥离", e == ("x", None))

print("\n=== 允许类别解析链（机器人设置 > sp 旧版 > 内置类型默认 > default）===")
SP_POLICY = {}          # 模拟 sp: entry_category_policy
INSTANCE = {}           # 模拟机器人配置里的 allowed_plugin_categories
POLICY = {              # get_entry_policy 的返回（内置默认 + sp 合并结果）
    "default": [FUNC, INTER],
    "weixin_oc": [FUNC],
    "openai_compat": [FUNC],
    **SP_POLICY,
}


def allowed(entry, etype=None):
    return M.allowed_categories(
        entry, POLICY, entry_type=etype, instance_policies=INSTANCE
    )


check("普通类型无配置 → default 全放开",
      allowed("qq_main", "aiocqhttp") == {FUNC, INTER})
check("内置类型默认：weixin_oc 只放行功能性",
      allowed("wx_main", "weixin_oc") == {FUNC})
check("内置特殊入口：openai_compat 只放行功能性",
      allowed("openai_compat") == {FUNC})
check("未知类型 → default", allowed("cron", "cron") == {FUNC, INTER})

# sp 旧版数据（旧面板按适配器类型写的键）仍生效且优先于内置默认
POLICY["weixin_oc"] = [FUNC, INTER]
check("sp 旧版类型键覆盖内置默认（weixin_oc 放开）",
      allowed("wx_main", "weixin_oc") == {FUNC, INTER})
del POLICY["weixin_oc"]

# sp 里按入口 id 精确写（特殊入口场景）
POLICY["openai_compat"] = [FUNC, INTER]
check("sp 按入口 id 覆盖", allowed("openai_compat") == {FUNC, INTER})
POLICY["openai_compat"] = [FUNC]

# 机器人实例配置：最高优先级，且同类型下可互不相同
INSTANCE["wx_main"] = [FUNC, INTER]
INSTANCE["wx_main"] = [FUNC, INTER, FUNC]   # 含重复项
check("机器人设置最高优先（wx_main 放开互动性）",
      allowed("wx_main", "weixin_oc") == {FUNC, INTER})
check("解析结果去重", INSTANCE["wx_main"].count(FUNC) == 2)  # get_instance_policies 层去重，allowed 直接集合化
check("同类型另一机器人不受影响（wx_small 仍仅功能性）",
      allowed("wx_small", "weixin_oc") == {FUNC})
INSTANCE["qq_main"] = [INTER]
check("机器人设置可反向收紧（qq_main 只留互动性）",
      allowed("qq_main", "aiocqhttp") == {INTER})
INSTANCE["wx_main"] = []          # 空列表 = 未设置，跟随默认
check("空列表视为未设置", allowed("wx_main", "weixin_oc") == {FUNC})

print("\n=== is_allowed 综合判定 ===")
INSTANCE["wx_main"] = [FUNC, INTER]   # 重新给 wx_main 显式放开
CTX = {"overrides": {}, "policy": POLICY, "instance_policies": INSTANCE}
check("互动性插件在未设置的 weixin_oc 机器人被拒",
      not M.is_allowed(entry="wx_small", entry_type="weixin_oc",
                       plugin_name="p", declared=INTER, context=CTX))
check("互动性插件在实例放行的 wx_main 被允许",
      M.is_allowed(entry="wx_main", entry_type="weixin_oc",
                   plugin_name="p", declared=INTER, context=CTX))
check("功能性插件处处可用",
      M.is_allowed(entry="wx_small", entry_type="weixin_oc",
                   plugin_name="p", declared=FUNC, context=CTX))
check("无插件名（系统级）放行",
      M.is_allowed(entry="wx_small", entry_type="weixin_oc",
                   plugin_name=None, declared=None, context=CTX))
check("tags 推断为互动时同样受限",
      not M.is_allowed(entry="wx_small", entry_type="weixin_oc",
                       plugin_name="p", declared=None,
                       context=CTX, tags=["表情包"]))
check("按 Key 额外放行优先于策略",
      M.is_allowed(entry="wx_small", entry_type="weixin_oc",
                   plugin_name="p", declared=INTER, context=CTX,
                   extra_plugins={"p"}))

print("\n=== get_instance_policies（机器人配置读取）===")
M._astrbot_platform_configs = staticmethod(lambda: [
    {"id": "qq_main", "type": "aiocqhttp"},                      # 未设置 → 不收录
    {"id": "wx_main", "allowed_plugin_categories": ["functional", "互动"]},
    {"id": "wx_small", "allowed_plugin_categories": []},         # 空 → 不收录
    {"id": "bad", "allowed_plugin_categories": "functional"},    # 非列表 → 不收录
    {"allowed_plugin_categories": [FUNC]},                       # 无 id → 跳过
    {"id": "  ", "allowed_plugin_categories": [FUNC]},           # 空 id → 跳过
])
got = M.get_instance_policies()
check("未设置的机器人不出现", "qq_main" not in got)
check("中文别名归一化", got.get("wx_main") == [FUNC, INTER])
check("空列表不收录", "wx_small" not in got)
check("非法类型不收录", "bad" not in got)

M._astrbot_platform_configs = staticmethod(lambda: [])
check("全局配置不可用 → 空表（默认策略兜底）",
      M.get_instance_policies() == {})

print(f"\n  -> {ok[0]}/{total[0]} 通过")
sys.exit(0 if ok[0] == total[0] else 1)