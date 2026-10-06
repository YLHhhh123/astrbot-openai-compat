import datetime

from sqlalchemy import case, func, select
from sqlmodel import col

from astrbot.api import sp, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.core.agent.runners.deerflow.constants import (
    DEERFLOW_PROVIDER_TYPE,
    DEERFLOW_THREAD_ID_KEY,
)
from astrbot.core.db.po import ProviderStat
from astrbot.core.platform.astr_message_event import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.active_event_registry import active_event_registry

THIRD_PARTY_AGENT_RUNNER_KEY = {
    "dify": "dify_conversation_id",
    "coze": "coze_conversation_id",
    "dashscope": "dashscope_conversation_id",
    DEERFLOW_PROVIDER_TYPE: DEERFLOW_THREAD_ID_KEY,
}
THIRD_PARTY_AGENT_RUNNER_STR = ", ".join(THIRD_PARTY_AGENT_RUNNER_KEY.keys())


class ConversationCommands:
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def _get_current_persona_id(self, session_id):
        curr = await self.context.conversation_manager.get_curr_conversation_id(
            session_id,
        )
        if not curr:
            return None
        conv = await self.context.conversation_manager.get_conversation(
            session_id,
            curr,
        )
        if not conv:
            return None
        return conv.persona_id

    async def _clear_third_party_runner_state(
        self,
        umo: str,
        agent_runner_type: str,
    ) -> None:
        session_key = THIRD_PARTY_AGENT_RUNNER_KEY.get(agent_runner_type)
        if not session_key:
            return
        await sp.remove_async(
            scope="umo",
            scope_id=umo,
            key=session_key,
        )

    async def reset(self, message: AstrMessageEvent) -> None:
        """重置 LLM 会话上下文"""
        umo = message.unified_msg_origin
        cfg = self.context.get_config(umo=umo)
        agent_runner_type = cfg["provider_settings"]["agent_runner_type"]

        if agent_runner_type in THIRD_PARTY_AGENT_RUNNER_KEY:
            active_event_registry.stop_all(umo, exclude=message)
            await self._clear_third_party_runner_state(umo, agent_runner_type)
            message.set_result(
                MessageEventResult().message("✅ 重置对话成功。"),
            )
            return

        if not await self.context.get_using_provider_async(umo):
            message.set_result(
                MessageEventResult().message("😕 未找到任何 LLM 提供商。请先配置。"),
            )
            return

        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
        if not cid:
            message.set_result(
                MessageEventResult().message(
                    "😕 当前未处于对话状态。使用 /new 创建新对话。",
                ),
            )
            return

        active_event_registry.stop_all(umo, exclude=message)
        await self.context.conversation_manager.update_conversation(
            umo,
            cid,
            [],
        )
        message.set_extra("_clean_group_context_session", True)
        message.set_result(MessageEventResult().message("✅ 重置对话成功。"))

    async def stop(self, message: AstrMessageEvent) -> None:
        """停止当前会话正在运行的 Agent 任务"""
        umo = message.unified_msg_origin
        cfg = self.context.get_config(umo=umo)
        agent_runner_type = cfg["provider_settings"]["agent_runner_type"]

        if agent_runner_type in THIRD_PARTY_AGENT_RUNNER_KEY:
            stopped_count = active_event_registry.stop_all(umo, exclude=message)
        else:
            stopped_count = active_event_registry.request_agent_stop_all(
                umo,
                exclude=message,
            )

        if stopped_count > 0:
            message.set_result(
                MessageEventResult().message(
                    f"✅ 已请求停止 {stopped_count} 个正在运行的任务。",
                ),
            )
            return

        message.set_result(
            MessageEventResult().message("✅ 当前会话没有正在运行的任务。"),
        )

    async def new_conv(self, message: AstrMessageEvent) -> None:
        """创建并切换到新对话"""
        umo = message.unified_msg_origin
        cfg = self.context.get_config(umo=umo)
        agent_runner_type = cfg["provider_settings"]["agent_runner_type"]

        if agent_runner_type in THIRD_PARTY_AGENT_RUNNER_KEY:
            active_event_registry.stop_all(umo, exclude=message)
            await self._clear_third_party_runner_state(umo, agent_runner_type)
            message.set_result(
                MessageEventResult().message("✅ 已创建新对话。"),
            )
            return

        active_event_registry.stop_all(umo, exclude=message)
        cpersona = await self._get_current_persona_id(umo)
        cid = await self.context.conversation_manager.new_conversation(
            umo,
            message.get_platform_id(),
            persona_id=cpersona,
        )
        message.set_extra("_clean_group_context_session", True)
        message.set_result(
            MessageEventResult().message(f"✅ 已切换到新对话：新对话({cid[:4]})。"),
        )

    async def stats(self, message: AstrMessageEvent) -> None:
        """查看当前对话的 Token 用量统计"""
        umo = message.unified_msg_origin
        cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

        if not cid:
            message.set_result(
                MessageEventResult().message(
                    "❌ 当前未处于对话状态。使用 /new 创建新对话。",
                ),
            )
            return

        db = self.context.get_db()
        async with db.get_db() as session:
            result = await session.execute(
                select(
                    func.count(case((col(ProviderStat.id).is_not(None), 1))).label(
                        "record_count",
                    ),
                    func.coalesce(func.sum(ProviderStat.token_input_other), 0).label(
                        "total_input_other",
                    ),
                    func.coalesce(func.sum(ProviderStat.token_input_cached), 0).label(
                        "total_input_cached",
                    ),
                    func.coalesce(func.sum(ProviderStat.token_output), 0).label(
                        "total_output",
                    ),
                ).where(
                    col(ProviderStat.agent_type) == "internal",
                    col(ProviderStat.conversation_id) == cid,
                )
            )
            stats = result.one()

        if stats.record_count == 0:
            message.set_result(
                MessageEventResult().message("📊 当前对话暂无用量统计。"),
            )
            return

        total_input_other = stats.total_input_other
        total_input_cached = stats.total_input_cached
        total_output = stats.total_output
        total_tokens = total_input_other + total_input_cached + total_output

        ret = (
            f"📊 当前对话 Token 用量 (ID: {cid[:8]}...)\n"
            f"总计:          {total_tokens:,}\n"
            f"输入(缓存):    {total_input_cached:,}\n"
            f"输入(其他):    {total_input_other:,}\n"
            f"输出:          {total_output:,}\n"
        )
        message.set_result(MessageEventResult().message(ret))

    async def his(self, message: AstrMessageEvent, page: int = 1) -> None:
        """查看对话记录"""
        if not self.context.get_using_provider(message.unified_msg_origin):
            message.set_result(
                MessageEventResult().message("未找到任何 LLM 提供商。请先配置。"),
            )
            return

        size_per_page = 6

        conv_mgr = self.context.conversation_manager
        umo = message.unified_msg_origin
        session_curr_cid = await conv_mgr.get_curr_conversation_id(umo)

        if not session_curr_cid:
            session_curr_cid = await conv_mgr.new_conversation(
                umo,
                message.get_platform_id(),
            )

        contexts, total_pages = await conv_mgr.get_human_readable_context(
            umo,
            session_curr_cid,
            page,
            size_per_page,
        )

        parts = []
        for context in contexts:
            if len(context) > 150:
                context = context[:150] + "..."
            parts.append(f"{context}\n")

        history = "".join(parts)
        ret = (
            f"当前对话历史记录："
            f"{history or '无历史记录'}\n\n"
            f"第 {page} 页 | 共 {total_pages} 页\n"
            f"*输入 /history 2 跳转到第 2 页"
        )

        message.set_result(MessageEventResult().message(ret).use_t2i(False))

    async def convs(self, message: AstrMessageEvent, page: int = 1) -> None:
        """查看对话列表"""
        cfg = self.context.get_config(umo=message.unified_msg_origin)
        agent_runner_type = cfg["provider_settings"]["agent_runner_type"]
        if agent_runner_type in THIRD_PARTY_AGENT_RUNNER_KEY:
            message.set_result(
                MessageEventResult().message(
                    f"{THIRD_PARTY_AGENT_RUNNER_STR} 对话列表功能暂不支持。",
                ),
            )
            return

        size_per_page = 6
        conversations_all = await self.context.conversation_manager.get_conversations(
            message.unified_msg_origin,
        )
        total_pages = (len(conversations_all) + size_per_page - 1) // size_per_page
        page = max(1, min(page, total_pages))
        start_idx = (page - 1) * size_per_page
        end_idx = start_idx + size_per_page
        conversations_paged = conversations_all[start_idx:end_idx]

        parts = ["对话列表：\n---\n"]
        global_index = start_idx + 1

        _titles = {}
        for conv in conversations_all:
            title = conv.title if conv.title else "新对话"
            _titles[conv.cid] = title

        provider_settings = cfg.get("provider_settings", {})
        platform_name = message.get_platform_name()
        for conv in conversations_paged:
            (
                persona_id,
                _,
                force_applied_persona_id,
                _,
            ) = await self.context.persona_manager.resolve_selected_persona(
                umo=message.unified_msg_origin,
                conversation_persona_id=conv.persona_id,
                platform_name=platform_name,
                provider_settings=provider_settings,
            )
            if persona_id == "[%None]":
                persona_name = "无"
            elif persona_id:
                persona_name = persona_id
            else:
                persona_name = "无"

            if force_applied_persona_id:
                persona_name = f"{persona_name} (自定义规则)"

            title = _titles.get(conv.cid, "新对话")
            parts.append(
                f"{global_index}. {title}({conv.cid[:4]})\n  人格情景: {persona_name}\n  上次更新: {datetime.datetime.fromtimestamp(conv.updated_at).strftime('%m-%d %H:%M')}\n"
            )
            global_index += 1

        parts.append("---\n")
        ret = "".join(parts)
        curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
            message.unified_msg_origin,
        )
        if curr_cid:
            title = _titles.get(curr_cid, "新对话")
            ret += f"\n当前对话: {title}({curr_cid[:4]})"
        else:
            ret += "\n当前对话: 无"

        cfg = self.context.get_config(umo=message.unified_msg_origin)
        unique_session = cfg["platform_settings"]["unique_session"]
        if unique_session:
            ret += "\n会话隔离粒度: 个人"
        else:
            ret += "\n会话隔离粒度: 群聊"

        ret += f"\n第 {page} 页 | 共 {total_pages} 页"
        ret += "\n*输入 /ls 2 跳转到第 2 页"

        message.set_result(MessageEventResult().message(ret).use_t2i(False))
        return

    async def groupnew_conv(self, message: AstrMessageEvent, sid: str = "") -> None:
        """创建新群聊对话"""
        if sid:
            session = str(
                MessageSession(
                    platform_name=message.platform_meta.id,
                    message_type=MessageType("GroupMessage"),
                    session_id=sid,
                ),
            )

            cpersona = await self._get_current_persona_id(session)
            cid = await self.context.conversation_manager.new_conversation(
                session,
                message.get_platform_id(),
                persona_id=cpersona,
            )
            message.set_result(
                MessageEventResult().message(
                    f"群聊 {session} 已切换到新对话: 新对话({cid[:4]})。",
                ),
            )
        else:
            message.set_result(
                MessageEventResult().message("请输入群聊 ID。/groupnew 群聊ID。"),
            )

    async def switch_conv(
        self,
        message: AstrMessageEvent,
        index: int | None = None,
    ) -> None:
        """通过 /ls 前面的序号切换对话"""
        if not isinstance(index, int):
            message.set_result(
                MessageEventResult().message("类型错误，请输入数字对话序号。"),
            )
            return

        if index is None:
            message.set_result(
                MessageEventResult().message(
                    "请输入对话序号。/switch 对话序号。/ls 查看对话 /new 新建对话",
                ),
            )
            return
        conversations = await self.context.conversation_manager.get_conversations(
            message.unified_msg_origin,
        )
        if index > len(conversations) or index < 1:
            message.set_result(
                MessageEventResult().message("对话序号错误，请使用 /ls 查看"),
            )
        else:
            conversation = conversations[index - 1]
            title = conversation.title if conversation.title else "新对话"
            await self.context.conversation_manager.switch_conversation(
                message.unified_msg_origin,
                conversation.cid,
            )
            message.set_result(
                MessageEventResult().message(
                    f"切换到对话: {title}({conversation.cid[:4]})。",
                ),
            )

    async def rename_conv(self, message: AstrMessageEvent, new_name: str = "") -> None:
        """重命名对话"""
        if not new_name:
            message.set_result(MessageEventResult().message("请输入新的对话名称。"))
            return
        await self.context.conversation_manager.update_conversation_title(
            message.unified_msg_origin,
            new_name,
        )
        message.set_result(MessageEventResult().message("重命名对话成功。"))

    async def del_conv(self, message: AstrMessageEvent) -> None:
        """删除当前对话"""
        umo = message.unified_msg_origin
        cfg = self.context.get_config(umo=umo)

        agent_runner_type = cfg["provider_settings"]["agent_runner_type"]
        if agent_runner_type in THIRD_PARTY_AGENT_RUNNER_KEY:
            active_event_registry.stop_all(umo, exclude=message)
            await self._clear_third_party_runner_state(umo, agent_runner_type)
            message.set_result(MessageEventResult().message("重置对话成功。"))
            return

        session_curr_cid = (
            await self.context.conversation_manager.get_curr_conversation_id(umo)
        )

        if not session_curr_cid:
            message.set_result(
                MessageEventResult().message(
                    "当前未处于对话状态，请 /switch 序号 切换或 /new 创建。",
                ),
            )
            return

        active_event_registry.stop_all(umo, exclude=message)

        await self.context.conversation_manager.delete_conversation(
            umo,
            session_curr_cid,
        )

        ret = "删除当前对话成功。不再处于对话状态，使用 /switch 序号 切换到其他对话或 /new 创建。"
        message.set_extra("_clean_ltm_session", True)
        message.set_result(MessageEventResult().message(ret))
