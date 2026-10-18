"""提案存储、审批与落盘应用。

流程
----
    AI 调用 propose()  → 闸门通过 → 存为「待审」（原文件不动）
    管理员 approve()   → 备份原文件 → 应用新内容 → 语法校验
                                    → 失败自动回滚
    管理员 deploy()    → 面板点「应用并重启」，让内核重新加载

状态落在 ``data/plugin_data/astrbot_plugin_adapter_editor/``：

    proposals/<id>.json      提案元数据（含完整改后内容，便于 diff 与回放）
    backups/<ts>-<rel>       每次应用前的原文件备份，永久保留
    audit.log                追加式审计日志

安全要点
--------
* **批准与落盘分离**：批准只把内容写进提案记录，仍不碰真实文件；
  真正覆盖原文件发生在 ``apply_to_disk()``，由面板显式触发。
  这样即使批准逻辑有 bug，也不会在无人确认时改动运行中的适配器。
* **永不删除**：备份只增不删，且不提供"清空备份"入口。
* **审计不可绕过**：所有状态变化都写audit.log。
"""

from __future__ import annotations

import ast
import io
import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from astrbot.api import logger

from . import gates

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"


def plugin_data_dir() -> str:
    from astrbot.core.utils.astrbot_path import get_astrbot_path

    base = os.path.join(str(get_astrbot_path()), "data", "plugin_data")
    return os.path.join(base, "astrbot_plugin_adapter_editor")


def _proposals_dir() -> str:
    d = os.path.join(plugin_data_dir(), "proposals")
    os.makedirs(d, exist_ok=True)
    return d


def _backups_dir() -> str:
    d = os.path.join(plugin_data_dir(), "backups")
    os.makedirs(d, exist_ok=True)
    return d


def audit_path() -> str:
    d = plugin_data_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "audit.log")


def audit(action: str, **kw) -> None:
    """追加一条审计记录。"""
    rec = {"ts": time.time(), "action": action, **kw}
    try:
        with io.open(audit_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # 审计写失败只告警，不阻断主流程
        logger.warning(f"[adapter_editor] 审计写入失败：{exc}")


def repo_root() -> str:
    from astrbot.core.utils.astrbot_path import get_astrbot_path

    return str(get_astrbot_path())


def abs_path(rel: str) -> str:
    return os.path.join(repo_root(), rel.replace("/", os.sep))


def read_file(rel: str) -> str | None:
    p = abs_path(rel)
    if not os.path.isfile(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        return f.read()


@dataclass
class Proposal:
    id: str
    rel_path: str
    adapter: str
    reason: str
    author_id: str
    author_name: str
    new_content: str
    status: str = STATUS_PENDING
    created_at: float = field(default_factory=time.time)
    decided_by: str = ""
    decided_at: float = 0.0
    decide_note: str = ""
    applied_at: float = 0.0
    backup_path: str = ""
    error: str = ""
    origin_size: int = 0
    new_size: int = 0
    diff_lines: int = 0
    is_new_file: bool = False
    owner_id: str = ""  # 提案者；若其创建了新适配器，则该适配器归其所有

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        known = {k: d.get(k) for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def _proposal_file(pid: str) -> str:
    return os.path.join(_proposals_dir(), f"{pid}.json")


def save_proposal(p: Proposal) -> None:
    with io.open(_proposal_file(p.id), "w", encoding="utf-8") as f:
        json.dump(p.to_dict(), f, ensure_ascii=False, indent=2)


def load_proposal(pid: str) -> Proposal | None:
    path = _proposal_file(pid)
    if not os.path.isfile(path):
        return None
    with io.open(path, encoding="utf-8") as f:
        return Proposal.from_dict(json.load(f))


def list_proposals(status: str | None = None) -> list[Proposal]:
    out: list[Proposal] = []
    for fn in os.listdir(_proposals_dir()):
        if not fn.endswith(".json"):
            continue
        try:
            with io.open(os.path.join(_proposals_dir(), fn), encoding="utf-8") as f:
                p = Proposal.from_dict(json.load(f))
        except Exception:
            continue
        if status and p.status != status:
            continue
        out.append(p)
    out.sort(key=lambda x: x.created_at, reverse=True)
    return out


# ── 提案（AI 侧入口）────────────────────────────────────────────────────


def _count_usage(author_id: str) -> dict[str, int]:
    """统计某作者的本日/本会话提案数。"""
    now = time.time()
    day_start = now - 86400
    session_cut = now - 1800  # 会话窗口：30 分钟无交互视为新会话
    session = day = 0
    for p in list_proposals():
        if p.author_id != author_id:
            continue
        if p.created_at >= day_start:
            day += 1
        if p.created_at >= session_cut:
            session += 1
    return {"session": session, "day": day}


def propose(
    *,
    rel_path: str,
    new_content: str,
    reason: str,
    author_id: str,
    author_name: str,
    config: dict,
) -> dict[str, Any]:
    """AI 提交一个改动提案。全部门禁在此强制执行。

    Returns:
        {"ok": bool, "id": str|None, "errors": [...], "warnings": [...]}
    """
    errors: list[str] = []
    warnings: list[str] = []
    q = gates.Quota(
        per_session=int(config.get("quota_per_session") or 3),
        per_day=int(config.get("quota_per_day") or 10),
        max_diff_lines=int(config.get("quota_max_diff_lines") or 200),
        max_file_bytes=int(config.get("quota_max_file_bytes") or 400_000),
    )

    # ① 路径闸门
    try:
        adapter, inner = gates.resolve_adapter_target(rel_path)
        gates.check_protected(adapter, config.get("protected_adapters") or [])
    except gates.PathDenied as exc:
        errors.append(f"路径被拒：{exc}")
        adapter, inner = "", ""
        rel_path = gates.normalize_rel(rel_path)

    # ② 配额闸门
    if not errors:
        r = gates.check_quota(_count_usage(author_id), q)
        if not r.ok:
            errors.extend(r.reasons)

    # ③ 规模闸门
    old = read_file(rel_path)
    is_new = old is None
    if not errors:
        if len(new_content.encode("utf-8")) > q.max_file_bytes:
            errors.append(f"文件过大（上限 {q.max_file_bytes // 1024}KB）")
        if old is not None:
            diff_lines = _count_diff_lines(old, new_content)
            if diff_lines > q.max_diff_lines:
                errors.append(f"改动行数 {diff_lines} 超过上限 {q.max_diff_lines}")

    # ④ 出站调用闸门（仅改现有文件时比对；新建文件从严）
    if not errors:
        if is_new:
            added = gates.outbound_calls_in_source(new_content)
            if added:
                errors.append(f"新建适配器不得包含出站调用：{', '.join(sorted(set(added))[:5])}")
        else:
            added_out = gates.diff_adds_outbound(old, new_content)
            if added_out:
                errors.append(f"不得新增/修改出站调用：{', '.join(sorted(set(added_out))[:5])}")
            else:
                kept = gates.outbound_calls_in_source(new_content)
                if kept:
                    warnings.append(f"保留了原有出站调用：{', '.join(sorted(set(kept))[:5])}")

    # ⑤ 危险导入闸门
    if not errors:
        bad = gates.banned_imports_in_source(new_content)
        if bad:
            errors.append(f"含被禁的导入/调用：{', '.join(bad[:5])}")

    # ⑥ 语法闸门（避免把坏代码送进审批队列）
    if not errors:
        if not new_content.strip():
            errors.append("内容为空，不允许清空文件")
        elif rel_path.endswith(".py"):
            try:
                ast.parse(new_content)
            except SyntaxError as exc:
                errors.append(f"语法错误：第{exc.lineno}行 {exc.msg}")

    if errors:
        audit("propose_rejected", author=author_id, path=rel_path, errors=errors)
        return {"ok": False, "id": None, "errors": errors, "warnings": warnings}

    pid = f"prop_{int(time.time() * 1000)}_{abs(hash(author_id + rel_path)) % 10000:04d}"
    p = Proposal(
        id=pid,
        rel_path=rel_path,
        adapter=adapter,
        reason=reason or "",
        author_id=author_id,
        author_name=author_name or author_id,
        new_content=new_content,
        origin_size=len(old or ""),
        new_size=len(new_content),
        diff_lines=_count_diff_lines(old or "", new_content),
        is_new_file=is_new,
        owner_id=author_id if is_new else "",
    )
    save_proposal(p)
    audit("propose_ok", id=pid, author=author_id, path=rel_path, warnings=warnings)
    logger.info(f"[adapter_editor] 新提案 {pid}：{rel_path}（+{p.diff_lines} 行）")
    return {"ok": True, "id": pid, "errors": [], "warnings": warnings}


def _count_diff_lines(a: str, b: str) -> int:
    """粗略统计改动行数（新增 + 删除）。"""
    import difflib

    return sum(
        1
        for line in difflib.unified_diff(
            a.splitlines(), b.splitlines(), lineterm="", n=0
        )
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


# ── 审批 ───────────────────────────────────────────────────────────────


def approve(pid: str, approver_id: str, note: str = "") -> dict[str, Any]:
    p = load_proposal(pid)
    if p is None:
        return {"ok": False, "error": "提案不存在"}
    if p.status != STATUS_PENDING:
        return {"ok": False, "error": f"提案状态为 {p.status}，不可批准"}
    p.status = STATUS_APPROVED
    p.decided_by = approver_id
    p.decided_at = time.time()
    p.decide_note = note
    save_proposal(p)
    audit("approve", id=pid, approver=approver_id, note=note)
    return {"ok": True, "status": p.status}


def reject(pid: str, approver_id: str, note: str = "") -> dict[str, Any]:
    p = load_proposal(pid)
    if p is None:
        return {"ok": False, "error": "提案不存在"}
    if p.status != STATUS_PENDING:
        return {"ok": False, "error": f"提案状态为 {p.status}，不可驳回"}
    p.status = STATUS_REJECTED
    p.decided_by = approver_id
    p.decided_at = time.time()
    p.decide_note = note
    save_proposal(p)
    audit("reject", id=pid, approver=approver_id, note=note)
    return {"ok": True, "status": p.status}


# ── 落盘应用 + 备份 + 回滚 ─────────────────────────────────────────────


def _backup_suffix(rel: str) -> str:
    """备份文件名后缀：相对路径里的分隔符统一换成 ``__``。

    备份与回退查找必须用**同一个函数**，否则回退永远找不到备份。
    """
    return rel.replace("\\", "/").replace("/", "__")


def _backup(rel: str) -> str:
    """备份原文件（不存在则记录"新建"）。返回备份路径。

    文件名带毫秒 + 自增序号：同一秒内可能连续备份多次（应用、回退都会备份），
    只用秒级时间戳会撞名覆盖，导致「回退用的备份」被后一次覆盖掉。
    """
    old = read_file(rel)
    stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}"
    base = os.path.join(_backups_dir(), f"{stamp}__{_backup_suffix(rel)}")
    dest = base
    n = 0
    while os.path.exists(dest) or os.path.exists(dest + ".newfile"):
        n += 1
        dest = f"{base}_{n}"
    if old is None:
        with io.open(dest + ".newfile", "w", encoding="utf-8") as f:
            f.write("")
        return dest + ".newfile"
    if old is None:
        with io.open(dest + ".newfile", "w", encoding="utf-8") as f:
            f.write("")
        return dest + ".newfile"
    with io.open(dest, "w", encoding="utf-8") as f:
        f.write(old)
    return dest


def _rollback(rel: str, backup: str | None) -> None:
    if backup is None:
        return
    if backup.endswith(".newfile"):
        # 原本不存在 → 回滚即删除新建文件
        target = abs_path(rel)
        if os.path.isfile(target):
            os.remove(target)
        return
    with io.open(backup, encoding="utf-8") as f:
        content = f.read()
    target = abs_path(rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with io.open(target, "w", encoding="utf-8") as f:
        f.write(content)


def apply_to_disk(pid: str) -> dict[str, Any]:
    """把已批准的提案写入真实文件。语法/导入校验失败则自动回滚。

    注意：改完适配器代码需重启 AstrBot 才加载，面板会在成功后提示。
    """
    p = load_proposal(pid)
    if p is None:
        return {"ok": False, "error": "提案不存在"}
    if p.status != STATUS_APPROVED:
        return {"ok": False, "error": f"提案状态为 {p.status}，需先批准"}

    # 应用前再校验一次：批准到落盘之间，文件/规则可能已变
    if p.rel_path.endswith(".py"):
        try:
            ast.parse(p.new_content)
        except SyntaxError as exc:
            p.status = STATUS_FAILED
            p.error = f"落盘前语法校验失败：{exc.msg}"
            save_proposal(p)
            audit("apply_precheck_failed", id=pid, error=p.error)
            return {"ok": False, "error": p.error}

    backup = _backup(p.rel_path)
    target = abs_path(p.rel_path)
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with io.open(target, "w", encoding="utf-8") as f:
            f.write(p.new_content)
        # 编译校验：把语法错误挡在"文件已写但import 会炸"之前
        if p.rel_path.endswith(".py"):
            src = io.open(target, encoding="utf-8").read()
            compile(src, target, "exec")
    except Exception as exc:
        _rollback(p.rel_path, backup)
        p.status = STATUS_FAILED
        p.error = f"写入失败已回滚：{exc}"
        save_proposal(p)
        audit("apply_failed_rolledback", id=pid, error=str(exc))
        return {"ok": False, "error": p.error}

    p.status = STATUS_APPLIED
    p.applied_at = time.time()
    p.backup_path = backup or ""
    p.error = ""
    save_proposal(p)
    audit("apply_ok", id=pid, path=p.rel_path, backup=backup or "")
    logger.info(f"[adapter_editor] 已应用 {pid} → {p.rel_path}（备份 {backup}）")
    return {"ok": True, "path": p.rel_path, "backup": backup, "need_restart": True}


def rollback_applied(pid: str, approver_id: str) -> dict[str, Any]:
    """把已应用的提案回退到应用前状态。

    备份文件名是 ``<时间戳>__<相对路径（/ 换成 __）>``，所以匹配时要用
    同样的替换规则，不能直接拿带斜杠的路径去 ``endswith``。
    """
    p = load_proposal(pid)
    if p is None:
        return {"ok": False, "error": "提案不存在"}
    if p.status != STATUS_APPLIED:
        return {"ok": False, "error": "仅已应用的提案可回退"}

    # 优先用「本提案应用时记录的那份备份」—— 备份文件名只精确到秒，
    # 同一秒内产生多份时按 mtime 挑选并不可靠。
    target_backup = p.backup_path if p.backup_path else ""
    if not (target_backup and os.path.isfile(target_backup)):
        suffix = _backup_suffix(p.rel_path)
        cands = [
            os.path.join(_backups_dir(), fn)
            for fn in os.listdir(_backups_dir())
            if fn.endswith(suffix) and ".newfile" not in fn
        ]
        if not cands:
            return {"ok": False, "error": "找不到可用备份"}
        target_backup = min(cands, key=os.path.getmtime)

    # 回退前先备份当前版本，保证「回退」本身也可再回退
    _backup(p.rel_path)
    earliest = target_backup
    try:
        with io.open(earliest, encoding="utf-8") as f:
            content = f.read()
        with io.open(abs_path(p.rel_path), "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as exc:
        return {"ok": False, "error": f"回退失败：{exc}"}

    # 状态保持 applied：文件已回退，但"这个提案曾被应用过"是事实，
    # 改成 pending 会让审计链断掉（apply → rollback → applied 无法追踪）。
    p.error = f"已回退（使用备份 {os.path.basename(earliest)}）"
    save_proposal(p)
    audit("rollback", id=pid, approver=approver_id, backup=os.path.basename(earliest))
    return {"ok": True, "backup": os.path.basename(earliest)}


def diff_of(p: Proposal) -> str:
    """生成 unified diff 供面板展示。

    ⚠️ 必须用**应用时的备份**作为 before，而不能读当前文件：
    提案应用后当前文件已等于新内容，直接读会得到空 diff。
    没有备份（新文件）时 before 为空字符串。
    """
    import difflib

    before = ""
    bk = p.backup_path
    if bk and os.path.isfile(bk) and not bk.endswith(".newfile"):
        try:
            with io.open(bk, encoding="utf-8") as f:
                before = f.read()
        except Exception:
            before = ""
    if not before and not p.is_new_file and not p.applied_at:
        # 尚未应用：当前文件就是改前版本
        before = read_file(p.rel_path) or ""

    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            p.new_content.splitlines(),
            fromfile=f"a/{p.rel_path}" + (" (新文件)" if p.is_new_file else ""),
            tofile=f"b/{p.rel_path}",
            lineterm="",
            n=3,
        )
    )
