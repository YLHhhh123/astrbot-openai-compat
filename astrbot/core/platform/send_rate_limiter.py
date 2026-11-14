"""Per-adapter outbound (send) rate limiting.

AstrBot 没有统一的出站漏斗：每个平台适配器的消息事件各自实现 ``send()``，
主动推送走 ``Platform.send_by_session()``。本模块把限速器挂在**适配器实例**上，
并在上述出站方法的子类实现外自动包一层等待，因此：

1. 新增平台适配器无需改任何代码，只要继承 ``Platform`` / ``AstrMessageEvent``；
2. 限速按适配器实例生效（一个机器人账号一个窗口），不按会话切分 —— 这正是
   平台侧风控关心的维度；
3. 配置写在 ``platform[i]["send_rate_limit"]``，可在 WebUI「机器人设置」里改。

配置结构::

    "send_rate_limit": {
        "enable": False,          # 是否启用
        "window_seconds": 60,     # 统计窗口（秒），0 表示不限窗口
        "max_messages": 20,       # 窗口内最多发送条数，0 表示不限条数
        "delay_seconds": 1.0,     # 相邻两条消息的最小间隔（秒）
    }
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import weakref
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .platform import Platform

logger = logging.getLogger("astrbot")

CONFIG_KEY = "send_rate_limit"
"""platform 条目里承载限速配置的键名。"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "enable": False,
    "window_seconds": 60,
    "max_messages": 20,
    "delay_seconds": 1.0,
}
"""缺省关闭；数值仅在 enable 为 True 时生效。"""

_adapters: weakref.WeakValueDictionary[str, Platform] = weakref.WeakValueDictionary()
"""platform_id -> 适配器实例，供消息事件反查自己所属适配器的限速器。"""


def _coerce_number(value: Any, default: float, *, as_float: bool) -> float | int:
    """把配置值安全转换为数字。

    Args:
        value: 原始配置值（可能来自 JSON、WebUI 或被手工改坏）。
        default: 转换失败时使用的缺省值。
        as_float: True 返回 float，False 返回 int。

    Returns:
        非负数值；非法输入回退到 ``default``。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0:
        return 0.0 if as_float else 0
    return number if as_float else int(number)


def normalize_settings(raw: Any) -> dict[str, Any]:
    """把任意输入归一化成完整的限速配置。

    Args:
        raw: 原始配置片段；非 dict 时整体回退到缺省值。

    Returns:
        含 ``enable`` / ``window_seconds`` / ``max_messages`` /
        ``delay_seconds`` 四个键的字典。
    """
    source = raw if isinstance(raw, dict) else {}
    return {
        "enable": bool(source.get("enable", DEFAULT_SETTINGS["enable"])),
        "window_seconds": _coerce_number(
            source.get("window_seconds", DEFAULT_SETTINGS["window_seconds"]),
            DEFAULT_SETTINGS["window_seconds"],
            as_float=False,
        ),
        "max_messages": _coerce_number(
            source.get("max_messages", DEFAULT_SETTINGS["max_messages"]),
            DEFAULT_SETTINGS["max_messages"],
            as_float=False,
        ),
        "delay_seconds": _coerce_number(
            source.get("delay_seconds", DEFAULT_SETTINGS["delay_seconds"]),
            DEFAULT_SETTINGS["delay_seconds"],
            as_float=True,
        ),
    }


def ensure_config_entry(platform_config: dict) -> bool:
    """确保 platform 条目带有完整的 ``send_rate_limit`` 配置。

    WebUI 只渲染条目里已经存在的键，因此读配置时要补齐；缺失字段按缺省值填。
    只补齐缺失项，不覆盖用户已保存的值。

    Args:
        platform_config: 单个 ``platform[i]`` 字典（就地修改）。

    Returns:
        True 表示补写或修正了字段。
    """
    raw = platform_config.get(CONFIG_KEY)
    normalized = normalize_settings(raw)
    if raw != normalized:
        platform_config[CONFIG_KEY] = normalized
        return True
    return False


def register_adapter(platform: Platform) -> None:
    """登记适配器实例，供消息事件按 platform id 反查。

    Args:
        platform: 刚完成 ``__init__`` 的平台适配器实例。
    """
    config = getattr(platform, "config", None)
    platform_id = config.get("id") if isinstance(config, dict) else None
    if platform_id:
        _adapters[str(platform_id)] = platform


def unregister_adapter(platform: Platform) -> None:
    """移除适配器登记（仅当登记的确实是这个实例时才删）。

    Args:
        platform: 即将销毁的平台适配器实例。
    """
    config = getattr(platform, "config", None)
    platform_id = config.get("id") if isinstance(config, dict) else None
    if platform_id and _adapters.get(str(platform_id)) is platform:
        del _adapters[str(platform_id)]


def resolve_limiter(platform_id: str | None) -> SendRateLimiter | None:
    """按 platform id 取到该适配器的限速器。

    Args:
        platform_id: ``PlatformMetadata.id``。

    Returns:
        该适配器的 :class:`SendRateLimiter`；适配器不存在时返回 None。
    """
    if not platform_id:
        return None
    adapter = _adapters.get(platform_id)
    if adapter is None:
        return None
    limiter = getattr(adapter, "send_rate_limiter", None)
    return limiter if isinstance(limiter, SendRateLimiter) else None


def _limiter_for(target: Any) -> SendRateLimiter | None:
    """为适配器实例或消息事件找到对应的限速器。

    Args:
        target: ``Platform`` 适配器实例，或 ``AstrMessageEvent`` 消息事件。

    Returns:
        对应适配器的限速器；找不到时返回 None。
    """
    limiter = getattr(target, "send_rate_limiter", None)
    if isinstance(limiter, SendRateLimiter):
        return limiter
    meta = getattr(target, "platform_meta", None)
    return resolve_limiter(getattr(meta, "id", None))


class SendRateLimiter:
    """单个平台适配器的出站限速器（滑动窗口 + 固定间隔）。

    语义：
        - ``window_seconds`` 窗口内最多发出 ``max_messages`` 条；
        - 相邻两条消息至少间隔 ``delay_seconds`` 秒；
        - 超出配额时**等待**而不是丢弃（消息攒住再发，不丢内容）；
        - 配置每次发送时从 platform 条目现读，WebUI 保存后适配器重载即生效。
    """

    def __init__(self, platform_config: dict | None = None) -> None:
        """初始化限速器。

        Args:
            platform_config: 该适配器的配置字典（``platform[i]``）。
        """
        self._platform_config = (
            platform_config if isinstance(platform_config, dict) else {}
        )
        self._lock = asyncio.Lock()
        self._timestamps: deque[float] = deque()
        self._last_sent: float | None = None
        self._last_log_at: float = 0.0
        self._held_tasks: set[asyncio.Task] = set()
        """已拿到配额的任务；同一条消息的后续出站调用不再重复计数。"""

    @property
    def settings(self) -> dict[str, Any]:
        """读取当前生效的限速配置。

        Returns:
            归一化后的 ``enable`` / ``window_seconds`` / ``max_messages`` /
            ``delay_seconds``。
        """
        return normalize_settings(self._platform_config.get(CONFIG_KEY))

    @property
    def enabled(self) -> bool:
        """是否真正启用了限速（开关打开且至少有一项限制非 0）。"""
        settings = self.settings
        if not settings["enable"]:
            return False
        return bool(
            settings["window_seconds"] > 0
            or settings["max_messages"] > 0
            or settings["delay_seconds"] > 0
        )

    def describe(self) -> dict[str, Any]:
        """返回可写入日志/接口的配置摘要。

        Returns:
            包含生效开关与各参数的字典。
        """
        settings = self.settings
        settings["enabled"] = self.enabled
        settings["pending_in_window"] = len(self._timestamps)
        return settings

    def _current_task(self) -> asyncio.Task | None:
        """返回当前协程任务（无事件循环时返回 None）。

        Returns:
            当前 ``asyncio.Task`` 或 None。
        """
        try:
            return asyncio.current_task()
        except RuntimeError:  # pragma: no cover - 无事件循环
            return None

    def is_holding_current_task(self) -> bool:
        """当前协程任务是否已经拿到配额（用于避免嵌套出站重复计数）。

        Returns:
            True 表示本次调用的上游已经计过一次。
        """
        task = self._current_task()
        return task is not None and task in self._held_tasks

    def mark_current_task(self) -> None:
        """把当前协程任务标记为已持有配额。"""
        task = self._current_task()
        if task is not None:
            self._held_tasks.add(task)

    def release_current_task(self) -> None:
        """释放当前协程任务的持有标记。"""
        task = self._current_task()
        if task is not None:
            self._held_tasks.discard(task)

    async def acquire(self) -> bool:
        """申请一个发送配额，必要时等待到窗口/间隔允许为止。

        Returns:
            是否真正走了限速逻辑（False 表示未启用，直接放行）。
        """
        settings = self.settings
        if not settings["enable"]:
            return False
        window = float(settings["window_seconds"])
        limit = int(settings["max_messages"])
        delay = float(settings["delay_seconds"])
        if window <= 0 and limit <= 0 and delay <= 0:
            return False

        async with self._lock:
            while True:
                now = time.monotonic()
                waited = 0.0
                if delay > 0 and self._last_sent is not None:
                    remaining = self._last_sent + delay - now
                    if remaining > 0:
                        waited = remaining
                        await asyncio.sleep(remaining)
                        now = time.monotonic()
                if window > 0 and limit > 0:
                    while self._timestamps and now - self._timestamps[0] >= window:
                        self._timestamps.popleft()
                    if len(self._timestamps) >= limit:
                        remaining = self._timestamps[0] + window - now + 0.05
                        if remaining > 0:
                            self._log_throttle(
                                remaining,
                                len(self._timestamps),
                                window,
                            )
                            await asyncio.sleep(remaining)
                            continue
                    self._timestamps.append(now)
                elif waited > 0:
                    self._log_throttle(waited, 0, window)
                self._last_sent = time.monotonic()
                return True

    def _log_throttle(self, seconds: float, count: int, window: float) -> None:
        """记录限速日志（10 秒内只打一条，避免刷屏）。

        Args:
            seconds: 本次需要等待的秒数。
            count: 当前窗口内已发送条数。
            window: 窗口长度（秒）。
        """
        now = time.monotonic()
        if now - self._last_log_at < 10:
            return
        self._last_log_at = now
        settings = self.settings
        logger.info(
            "Send rate limit active: waiting %.2fs (window=%ss, max=%s, "
            "delay=%ss, in_window=%s)",
            seconds,
            int(window),
            settings["max_messages"],
            settings["delay_seconds"],
            count,
        )


def wrap_send_method(cls: type, method_name: str) -> None:
    """把类自身定义的出站方法包一层限速等待（幂等、可重复调用）。

    只包装**子类自己定义**的方法：父类已经包过的方法被子类继承时不需要重复包装。
    只包 ``send`` / ``send_by_session`` 这类"真正投递一条消息"的方法；
    ``send_streaming`` 是同一消息的增量编辑，不计入条数。

    Args:
        cls: 待处理的类（``Platform`` 或 ``AstrMessageEvent`` 的子类）。
        method_name: 方法名，如 ``send`` / ``send_by_session``。
    """
    original = cls.__dict__.get(method_name)
    if original is None or getattr(original, "_astrbot_rate_limited", False):
        return
    if not asyncio.iscoroutinefunction(original):
        return

    @functools.wraps(original)
    async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        limiter = _limiter_for(self)
        if limiter is None or not limiter.enabled:
            return await original(self, *args, **kwargs)
        if limiter.is_holding_current_task():
            return await original(self, *args, **kwargs)
        await limiter.acquire()
        limiter.mark_current_task()
        try:
            return await original(self, *args, **kwargs)
        finally:
            limiter.release_current_task()

    wrapper._astrbot_rate_limited = True  # type: ignore[attr-defined]
    setattr(cls, method_name, wrapper)
