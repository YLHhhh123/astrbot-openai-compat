"""渠道能力绑定：把「某个机器人渠道」与它对应的账号级能力关联起来。

背景
----
部分插件需要**账号级**信息，而不是「当前消息是谁发的」：

* QQ 空间：需要机器人自己 QQ 号的 Cookie 才能发说说、看访客
* 这类信息既不属于某条消息，也不属于全局配置，天然属于**某个机器人渠道**

以前的写法是插件自己到处猜（``event.bot`` / ``event.platform`` / ``event.adapter``
挨个试 ``call_action``），既脆弱又难排错：换一个适配器、或者客户端对象换了包装，
插件就静默失效。

现在由内核统一提供：

1. 渠道配置里可以写绑定字段（见各适配器的 ``config_metadata``）
2. 插件通过 :func:`get_channel_binding` 按渠道取，取不到就是没配
3. 需要「从 OneBot 客户端现取」的场景走 :func:`call_onebot_action`

约定
----
绑定信息存放在渠道配置（``astrbot_config["platform"][i]``）里，
键名统一为 ``<能力名>_<字段>``，例如 QQ 空间的 ``qq_space_cookie``。
"""

from __future__ import annotations

import inspect
from typing import Any

from astrbot.core import logger

# 能力名 -> 该能力需要的字段（顺序即文档顺序）
CAPABILITY_FIELDS: dict[str, tuple[str, ...]] = {
    "qq_space": ("uin", "cookie", "cookie_domain"),
}

# 能力名 -> 展示名，供后台与日志使用
CAPABILITY_LABELS: dict[str, str] = {
    "qq_space": "QQ 空间",
}


def _platform_configs() -> list[dict]:
    """读取全局配置里的机器人（平台实例）配置列表。

    延迟导入 + 容错：单测或早期启动阶段全局配置可能不可用，
    此时返回空列表，调用方按「未绑定」处理。

    Returns:
        机器人渠道配置字典列表；不可用时为空列表。
    """
    try:
        from astrbot.core import astrbot_config
    except Exception:
        return []
    try:
        platforms = astrbot_config.get("platform", []) or []
    except Exception:
        return []
    return [item for item in platforms if isinstance(item, dict)]


def _field_key(capability: str, field: str) -> str:
    """拼接渠道配置里的字段名。

    Args:
        capability: 能力名，如 ``qq_space``。
        field: 字段名，如 ``cookie``。

    Returns:
        配置键名，如 ``qq_space_cookie``。
    """
    return f"{capability}_{field}"


def get_channel_binding(
    capability: str,
    *,
    platform_id: str | None = None,
) -> dict[str, Any] | None:
    """读取某个机器人渠道上的能力绑定。

    Args:
        capability: 能力名（见 :data:`CAPABILITY_FIELDS`）。
        platform_id: 机器人渠道 id（``platform_meta.id``）。
            为 ``None`` 时返回**第一个**配置了该能力的渠道。

    Returns:
        含 ``platform_id`` / ``platform_type`` 与各字段的字典；
        没有可用配置时返回 ``None``。
    """
    fields = CAPABILITY_FIELDS.get(capability)
    if fields is None:
        logger.warning("未知的渠道能力：%s", capability)
        return None

    fallback: dict[str, Any] | None = None
    for item in _platform_configs():
        bot_id = str(item.get("id") or "").strip()
        if platform_id is not None and bot_id != str(platform_id):
            continue
        values = {
            field: str(item.get(_field_key(capability, field)) or "").strip()
            for field in fields
        }
        if not any(values.values()):
            continue
        binding = {
            "platform_id": bot_id,
            "platform_type": str(item.get("type") or "").strip(),
            **values,
        }
        if platform_id is not None:
            return binding
        # 未指定渠道时保留第一个命中的，保持行为可预测
        if fallback is None:
            fallback = binding
    return fallback


def get_binding_for_event(capability: str, event: Any) -> dict[str, Any] | None:
    """按消息事件所属渠道读取能力绑定。

    Args:
        capability: 能力名。
        event: 当前消息事件。

    Returns:
        该渠道的绑定；渠道未配置时回落到「第一个配置了该能力的渠道」，
        都没有则返回 ``None``。
    """
    platform_id = None
    meta = getattr(event, "platform_meta", None) if event is not None else None
    if meta is not None:
        platform_id = str(getattr(meta, "id", "") or "").strip() or None
    if platform_id is None:
        try:
            platform_id = str(event.get_platform_id() or "").strip() or None
        except Exception:
            platform_id = None
    # 先按本渠道取；本渠道没配时再回落到任意已配置渠道
    # （单机多号时常见：只有主号配了 QQ 空间，其它渠道复用同一份）。
    binding = get_channel_binding(capability, platform_id=platform_id)
    if binding is None and platform_id is not None:
        binding = get_channel_binding(capability)
    return binding


async def call_onebot_action(client: Any, action: str, **params: Any) -> Any:
    """在 OneBot 客户端上调用一个 action，兼容几种常见调用形态。

    不同版本的 OneBot 实现把 action 调用挂在不同属性上（``call_action`` /
    ``send_action`` / 直接同名方法），这里统一收口，避免每个插件各写一套猜测。

    Args:
        client: OneBot 客户端对象。
        action: action 名，如 ``get_cookies``。
        **params: action 参数。

    Returns:
        action 的返回值。

    Raises:
        AttributeError: 客户端不提供任何可用的 action 调用入口。
        Exception: action 本身抛出的异常原样透传。
    """
    if client is None:
        raise AttributeError("OneBot 客户端为空")

    direct = getattr(client, action, None)
    if callable(direct):
        result = direct(**params)
        return await result if inspect.isawaitable(result) else result

    for attr in ("call_action", "send_action"):
        caller = getattr(client, attr, None)
        if not callable(caller):
            continue
        result = caller(action, **params)
        return await result if inspect.isawaitable(result) else result

    raise AttributeError(f"OneBot 客户端不支持 action 调用：{action}")


async def fetch_onebot_login_uin(client: Any) -> int:
    """读取 OneBot 客户端登录账号的 QQ 号。

    Args:
        client: OneBot 客户端对象。

    Returns:
        QQ 号；取不到时返回 ``0``。
    """
    for action in ("get_login_info", "get_login_info_async"):
        try:
            payload = await call_onebot_action(client, action)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        # 兼容 {user_id: x} / {data: {user_id: x}} 两种包装
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for key in ("user_id", "uin", "qq"):
            value = str(data.get(key) or "").strip()
            if value.isdigit():
                return int(value)
    return 0


def _platform_instances(context: Any = None) -> list[Any]:
    """列出当前已加载的平台适配器实例。

    内核没有全局的 platform manager 单例，实例挂在 ``Context`` 上；因此这里
    优先用调用方传入的 ``context``，其次尝试几个常见的历史挂载点。

    Args:
        context: 插件持有的 ``Context``；可为 ``None``。

    Returns:
        平台适配器实例列表；取不到时为空列表。
    """
    for holder in (context,):
        manager = getattr(holder, "platform_manager", None) if holder else None
        if manager is not None:
            for attr in ("platform_insts", "get_insts"):
                value = getattr(manager, attr, None)
                if callable(value):
                    try:
                        return list(value() or [])
                    except Exception:
                        continue
                if isinstance(value, list):
                    return list(value)
    return []


def iter_onebot_clients(context: Any = None) -> list[Any]:
    """列出平台实例上的 OneBot 客户端。

    插件过去需要沿 ``context`` → ``platform_manager`` → 各种属性名逐层试探，
    才能找到一个能 ``call_action`` 的对象。这里把同一件事收敛到内核：
    直接遍历已加载的平台实例，返回其中带 action 调用入口的对象。

    Args:
        context: 插件持有的 ``Context``（用于定位平台实例）。

    Returns:
        可用的 OneBot 客户端列表；没有时为空列表。
    """
    clients: list[Any] = []
    seen: set[int] = set()

    def add(candidate: Any) -> None:
        if candidate is None:
            return
        # 只收「看起来能调 action」的对象，避免把配置字典当客户端
        if not any(
            callable(getattr(candidate, attr, None))
            for attr in ("call_action", "send_action", "get_cookies")
        ):
            return
        marker = id(candidate)
        if marker in seen:
            return
        seen.add(marker)
        clients.append(candidate)

    for inst in _platform_instances(context):
        # 适配器把 OneBot 客户端挂在 `bot` 上（aiocqhttp 即是）
        add(getattr(inst, "bot", None))
        add(getattr(inst, "client", None))
        add(inst)
    return clients


def get_onebot_client(
    platform_id: str | None = None,
    *,
    context: Any = None,
) -> Any | None:
    """取某个渠道（或第一个可用渠道）的 OneBot 客户端。

    Args:
        platform_id: 机器人渠道 id；``None`` 时返回第一个可用客户端。
        context: 插件持有的 ``Context``（用于定位平台实例）。

    Returns:
        OneBot 客户端对象；没有可用客户端时返回 ``None``。
    """
    instances = _platform_instances(context)
    if platform_id is None:
        clients = iter_onebot_clients(context)
        return clients[0] if clients else None

    for inst in instances:
        meta = getattr(inst, "metadata", None)
        inst_id = str(getattr(meta, "id", "") or "").strip()
        if inst_id != str(platform_id):
            continue
        # 优先适配器挂的客户端对象；都没有时，仅当实例自身确实能调 action
        # 才把它当客户端返回（否则会把「配了但没连上」误判成可用）。
        for attr in ("bot", "client"):
            candidate = getattr(inst, attr, None)
            if candidate is not None:
                return candidate
        if any(
            callable(getattr(inst, attr, None))
            for attr in ("call_action", "send_action", "get_cookies")
        ):
            return inst
        return None
    return None
