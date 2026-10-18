"""内核补丁（feature_toggle 跳过逻辑）的单元测试。

补丁标记``lore-kernel-patch`` 勿删：升级后可据此重放，测试用于确认重放后的
解析逻辑与服务器历史行为一致。

在仓库根目录运行：``python _lore_patch_test.py``
"""

import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def check(ok, total, label, cond) -> None:
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def main() -> int:
    # 只导入补丁里的两个纯函数，避免拉起整个内核（那需要 astrbot 运行时）
    import importlib.util

    path = os.path.join(HERE, "astrbot", "core", "pipeline", "waking_check", "stage.py")
    src = open(path, encoding="utf-8").read()

    # 抽出补丁片段单独 exec：省掉模块级 import 与装饰器
    start = src.index("# --- lore-kernel-patch")
    end = src.index("@register_stage", start)
    snippet = src[start:end]
    # 去掉 from astrbot.api import sp（在测试里桩掉）
    snippet = snippet.replace("        from astrbot.api import sp\n", "")

    ns = {}
    exec(compile(snippet, "patch_snippet", "exec"), ns)
    extract = ns["_extract_feature"]

    ok, total = [0], [0]

    # ── 功能名解析 ────────────────────────────────────────────
    cases = [
        ("data.plugins.astrbot_plugin_ignore_user.main",
         "astrbot_plugin_ignore_user", "插件主模块"),
        ("data.plugins.astrbot_plugin_ignore_user.core.sub",
         "astrbot_plugin_ignore_user/sub", "core 子模块"),
        ("data.plugins.foo.main.core.bar.baz",
         "foo/bar", "core 下多层取第一段"),
        ("astrbot.builtin_stars.builtin_commands.main",
         None, "内置插件不参与"),
        ("not.a.plugin.path", None, "非 data.plugins 前缀"),
        ("data.plugins", None, "路径过短"),
        ("", None, "空路径"),
    ]
    for path_in, want, label in cases:
        got = extract(path_in)
        check(ok, total, f"{label}: {path_in!r}", got == want)

    # ── 开关判定（桩 sp）───���────────────────────────────────────
    store = {}

    class FakeSp:
        @staticmethod
        async def global_get(key, default=None):
            return store.get(key, default)

    class _Api:
        sp = FakeSp()

    class _Astrbot:
        api = _Api()

    ns["sp"] = FakeSp()
    enabled_fn = ns["_feature_enabled"]

    async def run():
        # 默认开启
        check(ok, total, "未配置时默认开启", await enabled_fn("p", None))
        # 全局禁用
        store["feature_toggle.global.disabled"] = ["p"]
        check(ok, total, "全局禁用生效", not await enabled_fn("p", None))
        check(ok, total, "全局禁用不影响其他功能",
              await enabled_fn("other", None))
        # 群级禁用（先清掉全局禁用，否则测的是全局而非群级）
        store["feature_toggle.global.disabled"] = []
        store["feature_toggle.group.123.disabled"] = ["p"]
        check(ok, total, "群级禁用生效", not await enabled_fn("p", "123"))
        check(ok, total, "群级禁用不影响他群",
              await enabled_fn("p", "456"))
        # 全局禁用确实对所有群生效
        store["feature_toggle.global.disabled"] = ["p"]
        check(ok, total, "全局禁用对所有群生效",
              not await enabled_fn("p", "456"))
        # 数据类型异常时容错
        store["feature_toggle.global.disabled"] = "oops"
        check(ok, total, "数据类型异常时容错为开启", await enabled_fn("p", None))

    asyncio.run(run())

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(main())
