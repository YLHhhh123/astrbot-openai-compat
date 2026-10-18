"""提案流端到端测试：备份、应用、失败回滚、审批流转。

用临时目录做仓库根，避免动真实适配器文件。"""

import io
import json
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGINS = os.path.dirname(HERE)          # .../data/plugins
sys.path.insert(0, PLUGINS)
PKG = "astrbot_plugin_adapter_editor"

# 桩掉 astrbot，让 proposal 能独立跑
if "astrbot" not in sys.modules:
    _a = types.ModuleType("astrbot"); _a.__path__ = []
    _core = types.ModuleType("astrbot.core"); _core.__path__ = []
    _logger = types.SimpleNamespace(
        warning=lambda *a, **k: None, info=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    _core.logger = _logger
    class _L:
        warning = staticmethod(lambda *a, **k: None)
        info = staticmethod(lambda *a, **k: None)
        debug = staticmethod(lambda *a, **k: None)
    _core.logger = _L()
    _path_mod = types.ModuleType("astrbot.core.utils.astrbot_path")

    _TEMP = tempfile.mkdtemp(prefix="adapter_editor_test_")
    _path_mod.get_astrbot_path = lambda: _TEMP
    _path_mod.get_astrbot_config_path = lambda: os.path.join(_TEMP, "data", "config")
    os.makedirs(os.path.join(_TEMP, "data", "config"), exist_ok=True)
    _api = types.ModuleType("astrbot.api")
    _api.logger = _L()
    _a.api = _api
    sys.modules.update({
        "astrbot": _a, "astrbot.api": _api, "astrbot.core": _core,
        "astrbot.core.utils": types.ModuleType("astrbot.core.utils"),
        "astrbot.core.utils.astrbot_path": _path_mod,
    })

from astrbot.core.utils.astrbot_path import get_astrbot_path  # noqa: E402
import importlib  # noqa: E402

# 以顶层包方式导入（data/ 没有 __init__.py，走 data.plugins.* 会失败）
prop = importlib.import_module(f"{PKG}.proposal")
gates = importlib.import_module(f"{PKG}.gates")


def check(ok, total, label, cond) -> None:
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


REL = "astrbot/core/platform/sources/testad/testad_platform_adapter.py"


def seed_original() -> None:
    p = os.path.join(str(get_astrbot_path()), REL.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    io.open(p, "w", encoding="utf-8").write(
        "async def handle(msg):\n"
        "    await self.bot.send_msg(1, msg)\n"
        "    return msg\n"
    )


def read_target() -> str:
    return io.open(
        os.path.join(str(get_astrbot_path()), REL.replace("/", os.sep)), encoding="utf-8"
    ).read()


CFG = {"protected_adapters": [], "quota_per_session": 99, "quota_per_day": 99,
       "quota_max_diff_lines": 500, "quota_max_file_bytes": 999999}


def main() -> int:
    ok, total = [0], [0]
    seed_original()

    # ① 合法提案进待审
    # 注意：改动必须**完全不动出站调用的实参**，否则按设计就该被拒。
    # 这里在 send 之前加一行纯本地赋值，出站签名保持 "self.bot.send_msg(1, msg)"。
    good = prop.propose(
        rel_path=REL,
        new_content="async def handle(msg):\n"
                    "    length = len(msg)\n"
                    "    await self.bot.send_msg(1, msg)\n"
                    "    return msg\n",
        reason="加一个长度统计",
        author_id="u1", author_name="用户1", config=CFG,
    )
    check(ok, total, "合法提案通过", good["ok"])
    pid = good["id"]

    # ② 原文件此刻必须没变
    check(ok, total, "提案不触碰原文件", "length = len(msg)" not in read_target())

    # ③ 未批准不能落盘
    res = prop.apply_to_disk(pid)
    check(ok, total, "未批准不可应用", not res["ok"])
    check(ok, total, "未应用时原文件仍未变", "length = len(msg)" not in read_target())

    # ④ 批准后可落盘
    ap = prop.approve(pid, "admin")
    check(ok, total, "批准成功", ap["ok"])
    check(ok, total, "重复批准被拒", not prop.approve(pid, "admin")["ok"])
    apd = prop.apply_to_disk(pid)
    check(ok, total, "应用成功", apd["ok"])
    check(ok, total, "原文件已更新", "length = len(msg)" in read_target())
    check(ok, total, "应用后标记 need_restart", apd.get("need_restart") is True)

    # ⑤ 备份存在且内容为改前版本
    bk = apd.get("backup")
    check(ok, total, "备份文件存在", bk and os.path.isfile(bk))
    if bk:
        check(ok, total, "备份是改前内容",
              "length = len(msg)" not in io.open(bk, encoding="utf-8").read())

    # ⑤b 改出站实参（含无害的局部变量替换）也必须被拒
    argchange = prop.propose(
        rel_path=REL,
        new_content="async def handle(msg):\n"
                    "    text = msg.strip()\n"
                    "    await self.bot.send_msg(1, text)\n"
                    "    return msg\n",
        reason="改用 strip 后的文本", author_id="u1", author_name="用户1", config=CFG,
    )
    check(ok, total, "改出站实参被拒", not argchange["ok"])

    # ⑥ 出站改动被闸门拦
    bad = prop.propose(
        rel_path=REL,
        new_content="async def handle(msg):\n"
                    "    await self.bot.send_msg(999999, msg)\n"
                    "    return msg\n",
        reason="改群号", author_id="u1", author_name="用户1", config=CFG,
    )
    check(ok, total, "改发送目标被拒", not bad["ok"])
    check(ok, total, "拒绝原因提到出站", any("出站" in e for e in bad["errors"]))

    # ⑦ 路径越界被拒
    for bad_path in ["astrbot/core/pipeline/whitelist_check/stage.py",
                     "data/plugins/x/main.py"]:
        r = prop.propose(rel_path=bad_path, new_content="x=1", reason="t",
                         author_id="u1", author_name="u", config=CFG)
        check(ok, total, f"路径越界被拒 {os.path.basename(bad_path)}", not r["ok"])

    # ⑧ 不可改清单
    r = prop.propose(rel_path=REL, new_content="# new\n", reason="t",
                     author_id="u1", author_name="u",
                     config={**CFG, "protected_adapters": ["testad"]})
    check(ok, total, "清单内适配器被拒", not r["ok"])

    # ⑨ 语法错误的提案不入队
    r = prop.propose(rel_path=REL, new_content="def f(:\n", reason="t",
                     author_id="u1", author_name="u", config=CFG)
    check(ok, total, "语法错误被拒", not r["ok"])

    # ⑩ 危险导入被拒
    r = prop.propose(rel_path=REL, new_content="import subprocess\nx=1\n", reason="t",
                     author_id="u1", author_name="u", config=CFG)
    check(ok, total, "危险导入被拒", not r["ok"])

    # ⑪ 驳回流转
    r2 = prop.propose(rel_path=REL, new_content="# 另一个改动\n", reason="t",
                      author_id="u1", author_name="u", config=CFG)
    if r2["ok"]:
        rj = prop.reject(r2["id"], "admin", "不合适")
        check(ok, total, "驳回成功", rj["ok"])
        check(ok, total, "驳回后不可应用", not prop.apply_to_disk(r2["id"])["ok"])
        check(ok, total, "驳回后原文件未变", "另一个改动" not in read_target())

    # ⑭ diff 生成（应用后仍须能看到改动 —— 用应用时的备份当 before）
    p_obj = prop.load_proposal(pid)
    d = prop.diff_of(p_obj)
    check(ok, total, "已应用后 diff 仍含新增行",
          "+" in d and "length = len(msg)" in d)

    # ⑭b 待审状态也能看到 diff（此时读当前文件作 before）
    pend = prop.propose(
        rel_path=REL,
        new_content="async def handle(msg):\n"
                    "    tag = 'v2'\n"
                    "    await self.bot.send_msg(1, msg)\n"
                    "    return msg\n",
        reason="加个标签", author_id="u1", author_name="u", config=CFG,
    )
    if pend["ok"]:
        pd = prop.diff_of(prop.load_proposal(pend["id"]))
        check(ok, total, "待审提案 diff 含新增行", "+" in pd and "tag = 'v2'" in pd)
        prop.reject(pend["id"], "admin", "测试清理")

    # ⑫ 回退已应用的改动
    rb = prop.rollback_applied(pid, "admin")
    check(ok, total, "回退成功", rb["ok"])
    check(ok, total, "回退后恢复原内容", "length = len(msg)" not in read_target())

    # ⑬ 审计日志有记录
    ap_ = prop.audit_path()
    os.makedirs(os.path.dirname(ap_), exist_ok=True)
    log = io.open(ap_, encoding="utf-8").read().strip().splitlines()
    check(ok, total, "审计日志非空", len(log) > 3)
    actions = {json.loads(l)["action"] for l in log if l.strip()}
    check(ok, total, "审计含 propose_ok", "propose_ok" in actions)
    check(ok, total, "审计含 approve", "approve" in actions)
    check(ok, total, "审计含 apply_ok", "apply_ok" in actions)
    check(ok, total, "审计含 rollback", "rollback" in actions)
    check(ok, total, "审计含拒绝记录", "propose_rejected" in actions)

    # ⑮ 列表过滤
    check(ok, total, "按状态过滤", all(
        x.status == prop.STATUS_APPLIED
        for x in prop.list_proposals(prop.STATUS_APPLIED)
    ))

    shutil.rmtree(_TEMP, ignore_errors=True)
    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(main())
