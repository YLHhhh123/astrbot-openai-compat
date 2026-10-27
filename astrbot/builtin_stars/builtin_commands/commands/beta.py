"""内测准入相关指令。

- ``/内测 <内测码>``：兑换一次性内测码，激活后即可使用对应内测插件
- ``/内测状态``：查看自己当前的内测资格

内测插件名单、白名单用户与提示语由后台「插件内测」页面维护
（内核 ``astrbot.core.star.beta_gate``）。
"""

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult


class BetaCommands:
    """内测资格相关命令。"""

    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def redeem(self, event: AstrMessageEvent, code: str) -> None:
        """兑换一次性内测码。"""
        from astrbot.core.star.beta_gate import ALL_PLUGINS, BetaGate

        raw = str(code or "").strip()
        if not raw:
            event.set_result(
                MessageEventResult().message(
                    "使用方法：/内测 <内测码>\n"
                    "内测码由管理员在后台「插件内测」页面生成。",
                ),
            )
            return

        uid = str(event.get_sender_id())
        ok, message, granted = await BetaGate.redeem(raw, uid)
        if not ok:
            event.set_result(MessageEventResult().message(message))
            return

        config = await BetaGate.get_config()
        scope_text = (
            "全部内测插件"
            if ALL_PLUGINS in granted
            else "、".join(granted) or "对应插件"
        )
        # redeem 返回的 message 区分「首次兑换」与「已激活过」，
        # 只有首次兑换才用配置里的成功提示，否则会误导用户以为换了新资格。
        headline = (
            config["redeem_success_message"] if message == "兑换成功。" else message
        )
        event.set_result(
            MessageEventResult().message(
                f"{headline}\n可用范围：{scope_text}",
            ),
        )

    async def status(self, event: AstrMessageEvent) -> None:
        """查看自己的内测资格。"""
        from astrbot.core.star.beta_gate import ALL_PLUGINS, BetaGate

        config = await BetaGate.get_config()
        if not config["enabled"]:
            event.set_result(
                MessageEventResult().message("当前没有正在进行的内测功能。"),
            )
            return

        context = await BetaGate.build_context(event)
        beta_plugins = sorted(context["beta_plugins"]) if context else []
        if not beta_plugins:
            event.set_result(
                MessageEventResult().message("当前没有正在进行的内测功能。"),
            )
            return

        uid = str(event.get_sender_id())
        granted = set(context["grant_plugins"]) if context else set()
        if ALL_PLUGINS in granted:
            granted_text = "全部内测插件（内测码兑换）"
        elif granted:
            granted_text = "、".join(sorted(granted))
        elif context and context["is_admin"]:
            granted_text = "管理员身份自动放行"
        elif context and uid in context["user_ids"]:
            granted_text = "白名单用户"
        else:
            granted_text = "无"

        event.set_result(
            MessageEventResult().message(
                f"内测插件（{len(beta_plugins)}）：{'、'.join(beta_plugins)}\n"
                f"你的资格：{granted_text}",
            ),
        )
