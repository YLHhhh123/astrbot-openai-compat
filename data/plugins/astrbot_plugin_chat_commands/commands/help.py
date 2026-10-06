"""帮助指令：列出本插件的全部指令，并标注当前用户对每条指令的权限。"""

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageEventResult

# 指令清单：(指令名, 描述)
COMMANDS: list[tuple[str, str]] = [
    ("help", "查看帮助与指令列表"),
    ("sid", "查看当前消息来源信息(UMO/UID/群ID)"),
    ("name", "设置当前会话(UMO)的展示别名"),
    ("reset", "重置当前会话的 LLM 上下文"),
    ("stop", "停止当前会话正在运行的 Agent 任务"),
    ("new", "创建并切换到新对话"),
    ("stats", "查看当前对话的 Token 用量统计"),
    ("dashboard_update", "更新 AstrBot WebUI 管理面板"),
    ("set", "设置会话变量(用于 Dify/Coze/DashScope 等输入)"),
    ("unset", "移除会话变量"),
    ("llm", "开启/关闭 LLM 聊天功能"),
    ("op", "授权管理员（默认普通超管）"),
    ("setop", "设置管理员等级：0=成员 1=普通 2=中级 3=最高"),
    ("deop", "取消管理员授权"),
    ("provider", "查看或切换 LLM Provider"),
    ("model", "查看或切换模型"),
    ("history", "查看对话记录"),
    ("ls", "查看对话列表"),
    ("groupnew", "为指定群聊创建新对话"),
    ("switch", "按序号切换对话"),
    ("rename", "重命名当前对话"),
    ("del", "删除当前对话"),
    ("persona", "查看或切换 Persona"),
    ("plugin ls", "查看已安装插件列表"),
    ("plugin off", "禁用插件"),
    ("plugin on", "启用插件"),
    ("plugin get", "安装插件"),
    ("plugin help", "查看插件帮助"),
]


class HelpCommand:
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def help(
        self,
        event: AstrMessageEvent,
        perm=None,
    ) -> None:
        """查看帮助与指令列表。"""
        lines: list[str] = []

        # 版本信息（失败时静默降级，不阻断帮助输出）
        try:
            from astrbot.core.config.default import VERSION
            from astrbot.core.dashboard_assets import get_dashboard_version

            dashboard_version = await get_dashboard_version()
            lines.append(f"AstrBot v{VERSION}(WebUI: {dashboard_version})")
        except Exception:
            lines.append("AstrBot")

        lines.append("")
        lines.append("可用指令：")
        lines.append("---")

        # 预解析当前角色，用于标注权限
        role = None
        if perm is not None:
            role = await perm.resolve_role(event)

        _ROLE_CN = {
            "top_admin": "最高超管",
            "mid_admin": "中级超管",
            "normal_admin": "普通超管",
            "group_owner": "群主",
            "group_admin": "群管理",
            "member": "普通成员",
        }
        if role is not None:
            lines.append(f"你当前的层级：{_ROLE_CN.get(role, role)}")

        for name, desc in COMMANDS:
            mark = ""
            if perm is not None and role is not None:
                mark = "" if perm.is_allowed(role, name) else " [无权限]"
            lines.append(f"/{name}: {desc}{mark}")

        lines.append("---")
        lines.append(
            "*提示：各指令的使用权限可在插件配置面板中按层级（三级超管/群主/群管理/普通成员）设置；"
            "留空=全部可用，填“禁用”=全部不可用。"
        )

        event.set_result(MessageEventResult().message("\n".join(lines)).use_t2i(False))
