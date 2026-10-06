"""超管分级查询 API（由内核内置插件 ``builtin_commands`` 提供）。

0/1/2/3 四级超管分级同步到 AstrBot 的 sp 全局存储
（键：``builtin_commands.tier_ids``），其他模块可通过本模块查询：

    from astrbot.builtin_stars.builtin_commands.tier_api import get_level
    level = await get_level(event)            # 0=普通成员 1=普通超管 2=中级超管 3=最高超管
    level = await get_level_by_id(user_id)

也可直接读取 sp 存储：

    data = await sp.global_get("builtin_commands.tier_ids", {})

data 结构：
    {"top_admin_ids": [...], "mid_admin_ids": [...], "normal_admin_ids": [...]}

命令级门槛装饰器 require_tier：

    @filter.command("xxx")
    @require_tier(2)          # 中级超管及以上可用；0/1/2/3 任选
    async def cmd(self, event, ...):
        yield event.plain_result(...)

兼容说明：本模块原属独立插件 ``builtin_commands_extension``，该插件已并入内核。
sp 键保留为 ``builtin_commands_extension.tier_ids`` 以便既有数据无缝沿用；
内核同时注册同名兼容模块 ``builtin_commands_extension.tier_api``，使旧插件的
``from builtin_commands_extension.tier_api import require_tier`` 仍可正常工作。
"""

import re

from functools import wraps

from astrbot.api import sp

_KEY = "builtin_commands_extension.tier_ids"

# 等级名称（用于权限不足提示）
TIER_NAMES = {0: "普通成员", 1: "普通超管", 2: "中级超管", 3: "最高超管"}


def _parse_ids(raw) -> list[str]:
    if not raw:
        return []
    return [s for s in re.split(r"[,，;；\s]+", str(raw)) if s]


def extract_uid(entry) -> str:
    """从超管注册名中提取纯数字 QQ 用户 ID。

    支持格式：
        '123456'                      -> '123456'
        '@名字(123456)'               -> '123456'
        '@1(123456)'                  -> '123456'
        'kong42900'（无 5 位以上数字）  -> 'kong42900'（原样返回）
    """
    s = str(entry).strip()
    m = re.search(r"\((\d{5,})\)", s)
    if m:
        return m.group(1)
    m = re.search(r"\d{5,}", s)
    if m:
        return m.group(0)
    return s


async def sync_tier_ids(config) -> None:
    """把三级超管 ID 列表同步到 sp 全局存储。

    由内置插件在加载时与每次 /op /setop /deop 变更后调用，供其他模块读取。
    """
    if not isinstance(config, dict):
        return
    data = {
        "top_admin_ids": _parse_ids(config.get("top_admin_ids")),
        "mid_admin_ids": _parse_ids(config.get("mid_admin_ids")),
        "normal_admin_ids": _parse_ids(config.get("normal_admin_ids")),
    }
    await sp.global_put(_KEY, data)


def load_tier_config(context) -> dict:
    """读取内置插件的分级配置，返回普通 dict。

    供**其他模块**（如会话指令集插件）复用同一份权限配置，避免各自读文件。
    内置插件自身通过构造参数拿到 ``AstrBotConfig`` 实例（可写），这里只给只读快照。
    """
    from pathlib import Path

    from astrbot.core.config import AstrBotConfig
    from astrbot.core.utils.astrbot_path import get_astrbot_config_path

    try:
        # 必须传 schema，否则 AstrBotConfig 会回退到主配置 cmd_config.json
        cfg = AstrBotConfig(
            config_path=str(
                Path(str(get_astrbot_config_path())) / "builtin_commands_config.json"
            ),
            schema=load_builtin_schema(),
        )
        return dict(cfg)
    except Exception:
        return {}


def load_builtin_schema() -> dict:
    """读取本插件的 ``_conf_schema.json``（保留空 dict 作为兜底）。"""
    import json as _json
    from pathlib import Path as _Path

    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_path

        path = (
            _Path(str(get_astrbot_path()))
            / "astrbot"
            / "builtin_stars"
            / "builtin_commands"
            / "_conf_schema.json"
        )
        with open(path, encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


async def get_level_by_id(user_id) -> int:
    """根据用户 ID 返回超管等级：0=普通成员 1=普通超管 2=中级超管 3=最高超管。

    支持 admin 注册名（如 '@静弦clear(2529283689)' / '@1(2717858671)'），
    通过 extract_uid 提取纯数字 ID 后匹配。
    """
    data = await sp.global_get(_KEY, {})
    if not isinstance(data, dict):
        data = {}
    uid = str(user_id)
    for entry in data.get("top_admin_ids", []):
        if uid == extract_uid(entry):
            return 3
    for entry in data.get("mid_admin_ids", []):
        if uid == extract_uid(entry):
            return 2
    for entry in data.get("normal_admin_ids", []):
        if uid == extract_uid(entry):
            return 1
    return 0


async def get_level(event) -> int:
    """返回事件发送者的超管等级（0-3）。"""
    return await get_level_by_id(event.get_sender_id())


def require_tier(min_level: int = 1):
    """命令级门槛装饰器：要求事件发送者超管等级 >= min_level。

    用法（AstrBot 插件命令，async generator 或 async 函数均可）：

        @filter.command("某命令")
        @require_tier(2)          # 需中级超管及以上
        async def cmd(self, event, ...):
            yield event.plain_result("...")

    min_level: 0=任意成员 1=普通超管 2=中级超管 3=最高超管。
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(self, event, *args, **kwargs):
            try:
                level = await get_level(event)
            except Exception:
                level = 0
            if level < min_level:
                yield event.plain_result(
                    f"❌ 权限不足：此指令需要 {TIER_NAMES.get(min_level, '超管')} 及以上权限。",
                )
                event.stop_event()
                return
            gen = func(self, event, *args, **kwargs)
            if hasattr(gen, "__aiter__"):
                async for item in gen:
                    yield item
            else:
                result = await gen
                if result is not None:
                    yield result

        return wrapper

    return decorator


def register_compat_modules() -> list[str]:
    """把本模块注册为旧插件路径 ``builtin_commands_extension.tier_api``。

    旧独立插件 ``builtin_commands_extension`` 已并入内核，其目录可以删除；
    但仍可能有插件写着 ``from builtin_commands_extension.tier_api import require_tier``。
    这里在 ``sys.modules`` 中登记同对象别名，使那些 import 继续可用，
    并且在旧插件目录确实存在时**不覆盖**真实插件（以真实插件优先）。

    Returns:
        实际登记的别名列表。
    """
    import sys
    import types

    registered: list[str] = []

    pkg_name = "builtin_commands_extension"
    api_name = f"{pkg_name}.tier_api"

    # 旧插件目录仍在时不要抢路，让真实插件自己解析
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_path

        from pathlib import Path

        legacy_dir = Path(str(get_astrbot_path())) / "data" / "plugins" / pkg_name
        if legacy_dir.is_dir():
            return []
    except Exception:
        # 路径探测失败时按"旧插件不存在"处理，继续登记别名
        pass

    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = []  # 标记为包
        pkg.__doc__ = (
            "兼容占位包：多层管理员功能已并入内置插件 builtin_commands。"
        )
        sys.modules[pkg_name] = pkg
    else:
        pkg = sys.modules[pkg_name]

    alias = types.ModuleType(api_name)
    alias.__dict__.update(
        {
            name: value
            for name, value in globals().items()
            if not name.startswith("__")
        }
    )
    sys.modules[api_name] = alias
    setattr(pkg, "tier_api", alias)
    registered.append(api_name)
    return registered
