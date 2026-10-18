"""适配器编辑闸门单元测试。

安全关键路径，改动前请先跑：``python _gates_test.py``
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gates  # noqa: E402


def check(ok, total, label, cond) -> None:
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def denied(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return False
    except gates.PathDenied:
        return True


SRC = "astrbot/core/platform/sources/telegram/telegram_platform_adapter.py"
META = "astrbot/core/platform/sources/myadapter/metadata.yaml"


def main() -> int:
    ok, total = [0], [0]

    # ── 闸门 1：路径 ──────────────────────────────────────────────
    check(ok, total, "允许平台适配器文件",
          gates.resolve_adapter_target(SRC) == ("telegram", "telegram_platform_adapter.py"))
    check(ok, total, "允许 metadata.yaml",
          gates.resolve_adapter_target(META) == ("myadapter", "metadata.yaml"))
    check(ok, total, "拒绝白名单管道",
          denied(gates.resolve_adapter_target, "astrbot/core/pipeline/whitelist_check/stage.py"))
    check(ok, total, "拒绝 provider",
          denied(gates.resolve_adapter_target, "astrbot/core/provider/manager.py"))
    check(ok, total, "拒绝 db",
          denied(gates.resolve_adapter_target, "astrbot/core/db/sqlite.py"))
    check(ok, total, "拒绝 star 注册",
          denied(gates.resolve_adapter_target, "astrbot/core/star/star_manager.py"))
    check(ok, total, "拒绝 dashboard api",
          denied(gates.resolve_adapter_target, "astrbot/dashboard/api/app.py"))
    check(ok, total, "拒绝 main.py",
          denied(gates.resolve_adapter_target, "astrbot/main.py"))
    check(ok, total, "拒绝平台外路径",
          denied(gates.resolve_adapter_target, "data/plugins/foo/main.py"))
    check(ok, total, "拒绝上级引用",
          denied(gates.resolve_adapter_target, "astrbot/core/platform/sources/../../etc/passwd"))
    check(ok, total, "拒绝空路径", denied(gates.resolve_adapter_target, ""))
    check(ok, total, "拒绝 pycache",
          denied(gates.resolve_adapter_target, "astrbot/core/platform/sources/x/__pycache__/a.py"))
    check(ok, total, "路径归一化",
          gates.resolve_adapter_target("./" + SRC)[0] == "telegram")

    # ── 闸门 2：不可改清单 ────────────────────────────────────────
    check(ok, total, "清单外放行", gates.check_protected("telegram", ["aiocqhttp"]) is None)
    check(ok, total, "清单内拒绝",
          denied(gates.check_protected, "aiocqhttp", ["aiocqhttp", "telegram"]))
    check(ok, total, "空清单放行", gates.check_protected("x", []) is None)

    # ── 闸门 3：出站调用 ──────────────────────────────────────────
    base = """
async def handle(msg):
    if msg:
        await self.bot.send_msg(group_id, text)
    return msg
"""
    same = base
    added = base + """
async def extra(msg):
    await self.bot.send_group_msg(999, "hi")
"""
    changed_arg = """
async def handle(msg):
    if msg:
        await self.bot.send_msg(group_id, text, extra="x")
    return msg
"""
    parse_only = """
def parse(text):
    return text.strip().lower()
"""
    check(ok, total, "未改出站 → 放行", gates.diff_adds_outbound(base, same) == [])
    check(ok, total, "新增出站 → 拒绝", len(gates.diff_adds_outbound(base, added)) > 0)
    check(ok, total, "改出站实参 → 拒绝", len(gates.diff_adds_outbound(base, changed_arg)) > 0)
    check(ok, total, "纯解析逻辑 → 放行", gates.diff_adds_outbound(base, parse_only) == [])
    check(ok, total, "删除出站 → 放行（不新增）", gates.diff_adds_outbound(added, base) == [])
    names = gates.outbound_calls_in_source(added)
    check(ok, total, "识别 send_msg", any("send_msg" in n for n in names))
    check(ok, total, "识别 send_group_msg", any("send_group_msg" in n for n in names))
    check(ok, total, "识别 call_api", gates.outbound_calls_in_source("async def f(): await bot.call_api('x')"))
    check(ok, total, "普通调用不误判",
          gates.outbound_calls_in_source("async def f(): return parse(text)") == [])
    check(ok, total, "多级属性调用",
          any("send" in n for n in gates.outbound_calls_in_source("async def f(): await a.b.c.send(1)")))
    # 最危险的一类：调用点数量没变，只把目标群号换了
    retarget = """
async def handle(msg):
    if msg:
        await self.bot.send_msg(114514, text)
    return msg
"""
    check(ok, total, "改发送目标群号 → 拒绝",
          len(gates.diff_adds_outbound(base, retarget)) > 0)
    check(ok, total, "改发送内容 → 拒绝",
          len(gates.diff_adds_outbound(base, base.replace("text", "other"))) > 0)
    check(ok, total, "签名包含实参",
          any("114514" in sig for _n, sig in gates.outbound_call_signatures(retarget)))
    check(ok, total, "语法错误时出站检测不崩", gates.outbound_calls_in_source("def f(:") == [])

    # ── 闸门 4：危险导入 ──────────────────────────────────────────
    check(ok, total, "拦 subprocess",
          gates.banned_imports_in_source("import subprocess") == ["import subprocess"])
    check(ok, total, "拦 from socket import",
          gates.banned_imports_in_source("from socket import socket") != [])
    check(ok, total, "拦 eval", gates.banned_imports_in_source("eval('1')") == ["eval()"])
    check(ok, total, "拦 __import__", gates.banned_imports_in_source("__import__('os')") != [])
    check(ok, total, "放行标准 import",
          gates.banned_imports_in_source("import json\nfrom typing import Any") == [])
    check(ok, total, "放行平台常用网络库（由出站闸门把控）",
          gates.banned_imports_in_source("import aiohttp\nimport requests") == [])
    check(ok, total, "拦 ctypes/shutil",
          len(gates.banned_imports_in_source("import ctypes\nimport shutil")) == 2)
    check(ok, total, "语法错误时导入检测返回标记",
          gates.banned_imports_in_source("def f(:") == ["<syntax error>"])

    # ── 闸门 5：配额 ──────────────────────────────────────────────
    q = gates.Quota(per_session=3, per_day=10)
    check(ok, total, "会话未超限", gates.check_quota({"session": 1, "day": 1}, q).ok)
    check(ok, total, "会话超限", not gates.check_quota({"session": 3, "day": 1}, q).ok)
    check(ok, total, "当日超限", not gates.check_quota({"session": 0, "day": 10}, q).ok)
    check(ok, total, "超限带原因", "上限" in gates.check_quota({"session": 3}, q).reasons[0])

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(main())
