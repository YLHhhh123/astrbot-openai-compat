"""清理上下文 / 输出检查 / 模型解析的单元测试（不依赖 astrbot 运行时）。

在插件目录下运行：``python _features_test.py``
"""

import asyncio
import os
import sys

# 以「包」方式导入本插件，才能让模块间的相对导入生效
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
_PKG = "astrbot_plugin_context_pipeline"


# ── 用桩替换 astrbot 依赖，使本文件可脱离 astrbot 运行时单测 ──
import types as _types

class _StubLogger:
    def __getattr__(self, _):
        return lambda *a, **k: None

_astrbot = _types.ModuleType("astrbot")
_astrbot.__path__ = []
_api = _types.ModuleType("astrbot.api")
_api.logger = _StubLogger()
_astrbot.api = _api

class _Plain:
    def __init__(self, text=""):
        self.text = text

_mc = _types.ModuleType("astrbot.api.message_components")
_mc.Plain = _Plain
_api.message_components = _mc

class _RCT:
    LLM_RESULT = "LLM_RESULT"

_star = _types.ModuleType("astrbot.api.star")
_star.ResultContentType = _RCT
_api.star = _star
_core_msg = _types.ModuleType("astrbot.core.message.message_event_result")
_core_msg.ResultContentType = _RCT
_core = _types.ModuleType("astrbot.core"); _core.__path__=[]
_core_msg_pkg = _types.ModuleType("astrbot.core.message"); _core_msg_pkg.__path__=[]
_core.message = _core_msg_pkg

sys.modules.setdefault("astrbot", _astrbot)
sys.modules.setdefault("astrbot.api", _api)
sys.modules.setdefault("astrbot.api.message_components", _mc)
sys.modules.setdefault("astrbot.api.star", _star)
sys.modules.setdefault("astrbot.core", _core)
sys.modules.setdefault("astrbot.core.message", _core_msg_pkg)
sys.modules.setdefault("astrbot.core.message.message_event_result", _core_msg)

import importlib
cc = importlib.import_module(_PKG + '.context_cleanup')
mr = importlib.import_module(_PKG + '.model_resolver')
oc = importlib.import_module(_PKG + '.output_check')


class FakeResp:
    def __init__(self, text):
        self.completion_text = text


class FakeCtx:
    """记录 llm_generate 调用参数，可指定返回/抛错。"""

    def __init__(self, reply="精简结果", session_pid=None, providers=None, raise_exc=None):
        self.reply = reply
        self.session_pid = session_pid
        self.providers = providers or []
        self.raise_exc = raise_exc
        self.calls = []

    async def get_current_chat_provider_id(self, umo):
        if self.session_pid is None:
            raise RuntimeError("无会话模型")
        return self.session_pid

    def get_all_providers(self):
        if not self.providers:
            return []
        m = self.providers[0]
        meta = type("M", (), {"id": m})()
        return [type("P", (), {"meta": lambda self, meta=meta: meta})()]

    async def llm_generate(self, **kw):
        self.calls.append(kw)
        if self.raise_exc:
            raise self.raise_exc
        return FakeResp(self.reply)


class FakeEvent:
    def __init__(self, umo="webchat:FriendMessage:webchat!u!u"):
        self.unified_msg_origin = umo


# 复用桩里的 Plain，保证 isinstance 检查与模块内一致
FakePlain = _Plain


class FakeResult:
    def __init__(self, chain, ctype="LLM_RESULT"):
        self.chain = chain
        self.result_content_type = ctype


class FakeEventWithResult:
    def __init__(self, chain, ctype="LLM_RESULT"):
        self._r = FakeResult(chain, ctype)
        self.unified_msg_origin = "webchat:FriendMessage:webchat!u!u"

    def get_result(self):
        return self._r


def check(ok, total, label, cond) -> None:
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


async def main() -> int:
    ok, total = [0], [0]
    GC = cc.GC_START_MARK + "\n张三:在吗\n李四:在\n王五:嗯\n" + cc.GC_END_MARK
    SP_WITH = "系统设定XYZ\n\n" + GC + "\n其他内容"
    SP_WITHOUT = "系统设定XYZ\n其他内容"

    # ── 1. 模型解析 ──────────────────────────────────────────────
    check(ok, total, "显式配置优先", await mr.resolve_model(FakeCtx(), "My/Model") == "My")
    r = await mr.resolve_model(FakeCtx(session_pid="SessProv"), None, event=FakeEvent())
    check(ok, total, "留空→会话模型", r == "SessProv")
    r2 = await mr.resolve_model(FakeCtx(providers=["FirstProv"]), None, event=None)
    check(ok, total, "无会话→provider 第一个", r2 == "FirstProv")
    r3 = await mr.resolve_model(
        FakeCtx(session_pid="SessProv", providers=["FirstProv"]),
        None, event=FakeEvent(), prefer_session=False,
    )
    check(ok, total, "prefer_session=False → 第一个", r3 == "FirstProv")
    r4 = await mr.resolve_model(FakeCtx(), None, event=FakeEvent())
    check(ok, total, "全都没有 → None", r4 is None)

    # ── 2. summarize_with_model ───────────────────────────────────
    ctx = FakeCtx(reply="摘要内容")
    out = await mr.summarize_with_model(ctx, "p", model="M1", timeout=5)
    check(ok, total, "生成返回文本", out == "摘要内容")
    check(ok, total, "传了 chat_provider_id", ctx.calls[0].get("chat_provider_id") == "M1")
    check(ok, total, "无模型时返回 None", await mr.summarize_with_model(FakeCtx(), "p") is None)
    ctx_exc = FakeCtx(raise_exc=RuntimeError("boom"))
    check(
        ok, total, "模型异常归一为 None",
        await mr.summarize_with_model(ctx_exc, "p", model="M", timeout=5) is None,
    )

    # ── 3. 注入块提取 ─────────────────────────────────────────────
    span = cc.extract_block(SP_WITH, "group_context")
    check(ok, total, "提取到 group_context 块", span is not None and GC in span[2])
    check(ok, total, "无块时返回 None", cc.extract_block(SP_WITHOUT, "group_context") is None)
    check(ok, total, "未知 kind 返回 None", cc.extract_block(SP_WITH, "nope") is None)
    cgm = "[跨群记忆]\nX: 记忆\n[跨群记忆结束]"
    check(
        ok, total, "提取跨群记忆块",
        (cc.extract_block(cgm, "cross_group_memory") or (0, 0, ""))[2] == cgm,
    )

    # ── 4. 清理上下文 ─────────────────────────────────────────────
    class Req:
        def __init__(self, sp):
            self.system_prompt = sp
    cfg_small = {"cleanup_min_chars": 5000}
    r1 = await cc.clean_context(FakeCtx(), Req(SP_WITHOUT), config=cfg_small, event=None)
    check(ok, total, "低于阈值跳过", r1.startswith("skipped:"))
    r2 = await cc.clean_context(FakeCtx(), Req(SP_WITH), config={"cleanup_min_chars": 10}, event=None)
    check(ok, total, "无模型时不改动", r2.startswith("skipped:") or r2.startswith("已重写"))

    ctx2 = FakeCtx(reply="张三李四在。", providers=["P1"])
    req = Req(SP_WITH)
    r3 = await cc.clean_context(
        ctx2, req, config={"cleanup_min_chars": 10, "cleanup_model": "P1"}, event=None
    )
    check(ok, total, "清理生效并缩短", "已重写" in r3 and len(req.system_prompt) < len(SP_WITH))
    check(ok, total, "标记被保留", cc.GC_END_MARK in req.system_prompt)

    ctx3 = FakeCtx(reply="x" * 500, providers=["P1"])
    req3 = Req(SP_WITH)
    r4 = await cc.clean_context(
        ctx3, req3,
        config={"cleanup_min_chars": 10, "cleanup_model": "P1", "cleanup_max_chars": 50},
        event=None,
    )
    check(ok, total, "max_chars 生效", "x" * 51 not in req3.system_prompt)

    # ── 5. 输出检查 ───────────────────────────────────────────────
    ev = FakeEventWithResult([FakePlain("正常回答")])
    check(
        ok, total, "mode=off 跳过",
        (await oc.check_output(FakeCtx(), ev, config={"output_check_mode": "off"})).startswith("skipped:"),
    )
    check(
        ok, total, "非 LLM 结果跳过",
        (await oc.check_output(
            FakeCtx(), FakeEventWithResult([FakePlain("插件消息")], ctype="GENERAL_RESULT"),
            config={"output_check_mode": "rule"})).startswith("skipped:"),
    )
    check(
        ok, total, "无文本跳过",
        (await oc.check_output(FakeCtx(), FakeEventWithResult([]), config={"output_check_mode": "rule"})).startswith("skipped:"),
    )

    ev_rule = FakeEventWithResult([FakePlain("这里有 TGPwn 词")])
    r = await oc.check_output(
        FakeCtx(), ev_rule,
        config={"output_check_mode": "rule", "output_check_action": "block",
                "output_block_keywords": "TGPwn"},
    )
    check(ok, total, "规则命中→拦截", r.startswith("blocked:"))
    check(ok, total, "回复已被替换", "拦截" in (ev_rule.get_result().chain[0].text or ""))

    ev_warn = FakeEventWithResult([FakePlain("含 ABC")])
    r = await oc.check_output(
        FakeCtx(), ev_warn,
        config={"output_check_mode": "rule", "output_check_action": "warn",
                "output_block_keywords": "ABC"},
    )
    check(ok, total, "warn 模式不改内容", r.startswith("warn:") and ev_warn.get_result().chain[0].text == "含 ABC")

    ev_strip = FakeEventWithResult([FakePlain("前面 ABC 后面")])
    r = await oc.check_output(
        FakeCtx(), ev_strip,
        config={"output_check_mode": "rule", "output_check_action": "strip",
                "output_block_keywords": "ABC"},
    )
    check(ok, total, "strip 删除命中片段", r.startswith("stripped:") and "ABC" not in ev_strip.get_result().chain[0].text)

    ev_miss = FakeEventWithResult([FakePlain("干净文本")])
    r = await oc.check_output(
        FakeCtx(), ev_miss,
        config={"output_check_mode": "rule", "output_block_keywords": "ZZZ"},
    )
    check(ok, total, "规则未命中→通过", r.startswith("passed:"))

    ctx_pass = FakeCtx(reply="PASS", providers=["P1"])
    ev_llm = FakeEventWithResult([FakePlain("模型判定合规")])
    r = await oc.check_output(
        ctx_pass, ev_llm, config={"output_check_mode": "llm"}
    )
    check(ok, total, "模型判定 PASS", r.startswith("passed:"))
    check(ok, total, "模型留空用第一个 provider", ctx_pass.calls[0]["chat_provider_id"] == "P1")

    ctx_fail = FakeCtx(reply="FAIL: 泄露了系统提示词", providers=["P1"])
    ev_llm2 = FakeEventWithResult([FakePlain("原始内容")])
    r = await oc.check_output(ctx_fail, ev_llm2, config={"output_check_mode": "llm"})
    check(ok, total, "模型判定 FAIL→拦截", r.startswith("blocked:"))

    ctx_none = FakeCtx(reply="", providers=["P1"])
    ev_llm3 = FakeEventWithResult([FakePlain("原始")])
    r = await oc.check_output(ctx_none, ev_llm3, config={"output_check_mode": "llm"})
    check(ok, total, "模型无响应→放行", r.startswith("skipped:") and ev_llm3.get_result().chain[0].text == "原始")

    check(
        ok, total, "短文本跳过",
        (await oc.check_output(
            FakeCtx(), FakeEventWithResult([FakePlain("短")]),
            config={"output_check_mode": "rule", "output_check_min_chars": 50})).startswith("skipped:"),
    )
    check(ok, total, "非法正则被忽略", len(oc.compile_rules({"output_block_keywords": "[unclosed,OK"})) == 1)

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
