"""适配器编辑：允许 AI 提交平台适配器改动提案，经管理员逐条审批后落盘。

安全设计（三层，缺一不可）
------------------------
1. **前端开关**（``config["enabled"]``，默认 False）：不开则 AI 的工具不可用。
2. **静态闸门**（:mod:`gates`）：路径、出站调用、危险导入、配额、规模。
   这部分在 :mod:`gates` 内实现并有单测，任何改动请先跑 ``_gates_test.py``。
3. **人工审批**：闸门通过也只是进入「待审」，真实文件要管理员在面板点批准、
   再点「应用并重启」才被覆盖，且每次覆盖前自动备份。

AI 侧只有 :func:`Main.tool_adapter_patch_propose` 一个入口，
拿不到 ``write_file`` / shell 之类的通用能力——这是刻意的：
约束必须在**工具边界**上成立，而不是靠提示词里写"请不要改白名单"。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import register

from . import proposal as P


@register(
    "astrbot_plugin_adapter_editor",
    "WorkBuddy",
    "适配器编辑：AI 可提交平台适配器改动提案，管理员逐条审批后落盘。含不可改清单、配额、审计与回退。",
    "1.0.0",
)
class AdapterEditor(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config: dict = config if isinstance(config, dict) else {}

    # ── 状态 ────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enabled", False))

    def get_status(self) -> dict[str, Any]:
        """供面板读取的完整状态。"""
        protected = self._protected()
        pending = P.list_proposals(P.STATUS_PENDING)
        approved = P.list_proposals(P.STATUS_APPROVED)
        applied = P.list_proposals(P.STATUS_APPLIED)
        rejected = P.list_proposals(P.STATUS_REJECTED)
        failed = P.list_proposals(P.STATUS_FAILED)
        return {
            "enabled": self.enabled,
            "approve_tier": str(self.config.get("approve_tier") or "top_admin"),
            "protected_adapters": sorted(protected),
            "allow_new_adapter": bool(self.config.get("allow_new_adapter", True)),
            "quota": {
                "per_session": int(self.config.get("quota_per_session") or 3),
                "per_day": int(self.config.get("quota_per_day") or 10),
                "max_diff_lines": int(self.config.get("quota_max_diff_lines") or 200),
            },
            "counts": {
                "pending": len(pending),
                "approved": len(approved),
                "applied": len(applied),
                "rejected": len(rejected),
                "failed": len(failed),
            },
        }

    def _protected(self) -> set[str]:
        raw = self.config.get("protected_adapters") or ""
        if isinstance(raw, list):
            return {str(x).strip() for x in raw if str(x).strip()}
        return {x.strip() for x in str(raw).replace("，", ",").split(",") if x.strip()}

    # ── AI 侧工具 ────────────────────────────────────────────────────

    @filter.llm_tool("adapter_patch_propose")
    async def tool_adapter_patch_propose(
        self,
        rel_path: str,
        new_content: str,
        reason: str,
        event: AstrMessageEvent = None,
    ) -> str:
        """向管理员提交一个平台适配器改动提案（需人工审批后才生效）。

        ⚠️ 两个格式约束，改动时务必遵守：
        1. 本 docstring 会被 docstring_parser 解析成工具 schema，每个**模型可见**参数
           必须写成 `名称(str): 说明`，否则内核报「缺少类型注释」。
        2. ``event`` 由内核在调用时注入，**不能**写进 docstring 的 Args，
           否则内核会尝试把它暴露给模型并因类型不支持而注册失败。

        Args:
            rel_path(str): 目标文件相对路径，格式为 astrbot/core/platform/sources/平台名/文件名.py 。新建适配器还需同目录的 metadata.yaml 。
            new_content(str): 目标文件的完整新内容，不是 diff 片段。
            reason(str): 改动理由，会展示给审批人。

        Returns:
            str: 提交结果说明；若被闸门拒绝会列出具体原因。
        """
        if not self.enabled:
            return "适配器编辑功能当前关闭。请让管理员在 WebUI「适配器编辑」面板开启后再试。"

        # 提案者身份以 event 为准，不能让 LLM 自填。
        sender = str(event.get_sender_id()) if event is not None else "unknown"
        name = str(event.get_sender_name() or sender) if event is not None else "unknown"

        # 「允许新建」开关：目标是尚不存在的文件时额外校验
        if not self.config.get("allow_new_adapter", True) and P.read_file(rel_path) is None:
            return "当前不允许新建适配器，只能修改已存在的适配器文件。"

        res = P.propose(
            rel_path=rel_path,
            new_content=new_content,
            reason=reason,
            author_id=sender,
            author_name=name,
            config=self.config,
        )
        if not res["ok"]:
            return "提案被闸门拒绝：\n- " + "\n- ".join(res["errors"])

        warn = ""
        if res["warnings"]:
            warn = "\n注意：" + "；".join(res["warnings"])
        return (
            f"提案已提交，等待管理员审批（编号 {res['id']}）。"
            f"批准并重启后才会生效。{warn}"
        )

    # ── 管理员命令（辅助面板之外的另一入口）──────────────────────────

    def _require_approver(self, event: AstrMessageEvent) -> bool:
        """校验调用者是否达到审批权限等级。"""
        try:
            from astrbot.builtin_stars.builtin_commands.tier_api import get_level_by_id

            need = str(self.config.get("approve_tier") or "top_admin")
            order = {"normal_admin": 1, "mid_admin": 2, "top_admin": 3}
            want = order.get(need, 3)
            got = get_level_by_id(str(event.get_sender_id()))
            return got >= want
        except Exception as exc:
            logger.warning(f"[adapter_editor] 权限校验异常，按拒绝处理：{exc}")
            return False

    @filter.command("适配器提案")
    async def cmd_list(self, event: AstrMessageEvent, status: str = "pending"):
        """查看适配器改动提案列表"""
        if not self._require_approver(event):
            event.set_result(MessageEventResult().message("权限不足：需要审批等级。"))
            return
        items = P.list_proposals(status or None)
        if not items:
            event.set_result(MessageEventResult().message(f"没有状态为 {status} 的提案。"))
            return
        lines = [f"适配器改动提案（{status}）共 {len(items)} 条："]
        for p in items[:10]:
            lines.append(
                f"  {p.id}｜{p.adapter}｜{'新建' if p.is_new_file else '修改'}｜"
                f"+{p.diff_lines}行｜{p.author_name}｜{p.reason[:30]}"
            )
        event.set_result(MessageEventResult().message("\n".join(lines)))

    @filter.command("适配器批准")
    async def cmd_approve(self, event: AstrMessageEvent, pid: str = "", note: str = ""):
        """批准一个提案（仍需在面板点「应用并重启」才落盘）"""
        if not self._require_approver(event):
            event.set_result(MessageEventResult().message("权限不足：需要审批等级。"))
            return
        res = P.approve(pid, str(event.get_sender_id()), note)
        if not res["ok"]:
            event.set_result(MessageEventResult().message(f"批准失败：{res['error']}"))
            return
        event.set_result(
            MessageEventResult().message(
                f"已批准 {pid}。**尚未落盘**，请在 WebUI 面板点「应用并重启」使其生效。"
            )
        )

    @filter.command("适配器驳回")
    async def cmd_reject(self, event: AstrMessageEvent, pid: str = "", note: str = ""):
        """驳回一个提案"""
        if not self._require_approver(event):
            event.set_result(MessageEventResult().message("权限不足：需要审批等级。"))
            return
        res = P.reject(pid, str(event.get_sender_id()), note)
        if not res["ok"]:
            event.set_result(MessageEventResult().message(f"驳回失败：{res['error']}"))
            return
        event.set_result(MessageEventResult().message(f"已驳回 {pid}。"))
