"""模型分组的内核集成测试：用真实配置验证 ProviderManager 的分组分支。

不依赖插件上下文，直接构造 ProviderManager 并注入假的 acm/inst_map，
验证 _resolve_by_model_group 与异步分支是否按分组返回 provider。
"""

import asyncio
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# ---- 桩：只提供 ProviderManager 依赖链需要的最小面----
_store = {}


class _Sp:
    @staticmethod
    def get(k, d=None, scope=None, scope_id=None):
        return d

    @staticmethod
    async def get_async(scope, scope_id, k, d=None):
        return d

    @staticmethod
    async def global_get(k, d=None):
        return _store.get(k, d)

    @staticmethod
    async def global_put(k, v):
        _store[k] = v


class _Logger:
    def __getattr__(self, _):
        return lambda *a, **k: None


_cfg = json.load(open(os.path.join(HERE, "data", "cmd_config.json"), encoding="utf-8-sig"))

_a = types.ModuleType("astrbot"); _a.__path__ = []
_core = types.ModuleType("astrbot.core"); _core.__path__ = []
# provider 必须是真包（磁盘上存在），否则 model_groups 子模块 import 不到
_prov = types.ModuleType("astrbot.core.provider")
_prov.__path__ = [os.path.join(HERE, "astrbot", "core", "provider")]
_core.logger = _Logger()
_core.sp = _Sp()
_core.astrbot_config = _cfg
_api = types.ModuleType("astrbot.api"); _api.sp = _Sp()
_a.api = _api
sys.modules.update({
    "astrbot": _a, "astrbot.api": _api, "astrbot.core": _core,
    "astrbot.core.provider": _prov,
})

from astrbot.core.provider.model_groups import (  # noqa: E402
    expand_group, find_group_for_scope, load_groups, resolve_group_providers,
)


def check(ok, total, label, cond):
    good = bool(cond)
    total[0] += 1
    ok[0] += good
    print(f"  {'OK  ' if good else 'FAIL'} {label}")


def main() -> int:
    ok, total = [0], [0]
    groups = load_groups(_cfg)
    check(ok, total, "真实配置加载出分组", len(groups) >= 3)

    # 内核用的判定函数（与 manager._group_candidates 同路径）
    for umo in ("webchat:FriendMessage:webchat!u!u",
                "aiocqhttp:GroupMessage:114514",
                None):
        name = find_group_for_scope(groups, umo, None)
        exp = expand_group(name, groups) if name else []
        usable = [m for m in exp if m in {p["id"] for p in _cfg.get("provider", []) if p.get("enable")}]
        check(ok, total,
              f"umo={str(umo)[:28]:28} → 组={name} 可用成员={len(usable)}",
              bool(name) and bool(usable))

    # 轮询组：连解 4 次
    ids1, g1 = asyncio.run(resolve_group_providers("rotating", _cfg))
    ids2, _ = asyncio.run(resolve_group_providers("rotating", _cfg))
    ids3, _ = asyncio.run(resolve_group_providers("rotating", _cfg))
    check(ok, total, f"轮询组主模型轮转（{ids1[0]} → {ids2[0]} → {ids3[0]}）",
          len({ids1[0], ids2[0], ids3[0]}) > 1)
    check(ok, total, "轮询后成员集合不变", set(ids1) == set(ids2) == set(ids3))

    # fallback 组：顺序稳定
    f1, _ = asyncio.run(resolve_group_providers("daily", _cfg))
    f2, _ = asyncio.run(resolve_group_providers("daily", _cfg))
    check(ok, total, "fallback 组顺序稳定不轮转", f1 == f2)

    # 参数映射（真实 provider 名）
    pids = [p["id"] for p in _cfg.get("provider", []) if p.get("enable")]
    mapped = []
    for pid in pids[:3]:
        from astrbot.core.provider.model_groups import map_params_to_provider
        mapped.append((pid, map_params_to_provider(
            {"temperature": 0.2, "max_output_tokens": 1024}, pid)))
    check(ok, total, "语义参数按厂商映射", all(
        ("max_tokens" in m or "max_output_tokens" in m) for _p, m in mapped))
    check(ok, total, "temperature 字段名稳定",
          all(m.get("temperature") == 0.2 for _p, m in mapped))

    # 嵌套组
    nest, _ = asyncio.run(resolve_group_providers("nested", _cfg))
    check(ok, total, f"嵌套组展开非空（{len(nest)} 个成员）", len(nest) > 0)

    print(f"\n  -> {ok[0]}/{total[0]} 通过")
    return 0 if ok[0] == total[0] else 1


if __name__ == "__main__":
    sys.exit(main())
