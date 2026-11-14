"""按机器人适配器（platform 实例）派生的配置档案。

背景：
    AstrBot 只有一份全局配置 ``data/cmd_config.json``，其中的 ``platform`` 数组
    每个元素是一个机器人适配器实例（QQ 官方机器人、OneBot、个人微信、Telegram…）。
    不同适配器对插件的可用范围往往不同，把这些差异直接手写进
    ``cmd_config.json`` 的 platform 条目有三个问题：

    1. 没有基线可对照，改坏了无法回溯；
    2. WebUI 保存机器人设置时会按自己的 schema 重写条目，手写字段可能被丢弃；
    3. 多个适配器之间无法并排审计。

本模块提供的能力：

    1. 档案目录 ``data/config_platforms/<platform_id>.json``，一份档案对应一个
       适配器实例（按 ``id`` 命名，与数组下标无关）；
    2. 档案的覆盖值基线来自 ``astrbot.core.config.default.DEFAULT_CONFIG``，
       **绝不**从已是运行态的 ``data/cmd_config.json`` 反向克隆 —— 否则一次
       "克隆"就把用户当前的运行配置固化成基线，后续再也分不清默认值与用户改动；
    3. :func:`apply_profiles` 把档案里的**白名单字段**合并进 ``platform[i]`` 条目
       与对应 ``platform_specific`` 段，按 ``id`` 匹配，幂等（无变化不写盘）；
    4. :func:`apply_profiles_to_running_config` 供启动流程调用：任何异常都被吞掉
       并记日志，档案目录缺失或档案损坏都不影响 AstrBot 启动。

合入白名单见 :data:`PLATFORM_OVERLAY_FIELDS`：只有这些键会被档案覆盖。
token / secret / appid / cookie 等运行时凭据既不会进入档案，也永远不会被档案覆盖
（整份覆盖更是被禁止的 —— 那会冲掉凭据）。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

logger = logging.getLogger("astrbot")

#: 档案目录名（位于 AstrBot data 目录下）。
PROFILE_DIR_NAME = "config_platforms"
#: 档案 schema 版本，写进 ``_meta.schema_version``。
SCHEMA_VERSION = 1
#: 档案元数据键。
META_KEY = "_meta"
#: ``platform_specific`` 覆盖段的键名（键为 DEFAULT_CONFIG 里的平台分组名，通常是适配器类型）。
PLATFORM_SPECIFIC_KEY = "platform_specific"

#: 允许从档案合并进 ``platform[i]`` 的字段白名单。
#:
#: 只放"策略类"字段：谁能用哪些插件、消息形态开关。``enable`` / ``id`` / ``type``
#: 以及 token / appid / secret / cookie 一律不在此列 —— 它们是运行状态与凭据，
#: 由用户与 WebUI 掌管，档案不许碰。
PLATFORM_OVERLAY_FIELDS: tuple[str, ...] = (
    # 插件闸门（本机制的主要目标）
    "allowed_plugin_categories",
    "allowed_plugins",
    # QQ 官方机器人 / 通用消息形态开关
    "use_markdown",
    "enable_group_c2c",
    "enable_guild_direct_message",
    # Telegram 专属行为开关
    "telegram_command_register",
    "telegram_command_auto_refresh",
)


def get_profile_dir(profile_dir: str | Path | None = None) -> Path:
    """返回档案目录路径。

    Args:
        profile_dir: 显式指定的目录（测试用）；``None`` 时取
            ``<AstrBot data>/config_platforms``。

    Returns:
        档案目录的 :class:`Path`。
    """
    if profile_dir is not None:
        return Path(profile_dir)
    return Path(get_astrbot_data_path()) / PROFILE_DIR_NAME


def default_config_fingerprint() -> str:
    """计算 ``DEFAULT_CONFIG`` 的稳定指纹。

    档案里记下派生时的基线指纹，``DEFAULT_CONFIG`` 随版本升级变化时可以据此
    发现"档案基线已过期"，而不是默默套用旧值。

    Returns:
        形如 ``sha256:<hex>`` 的字符串。
    """
    payload = json.dumps(
        DEFAULT_CONFIG,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_platform_specific(platform_type: str) -> dict[str, Any]:
    """返回 ``DEFAULT_CONFIG`` 里某适配器类型的 ``platform_specific`` 基线（深拷贝）。

    档案的 ``platform_specific`` 段必须从这份基线派生，而不是从用户当前的
    ``data/cmd_config.json`` 抄 —— 后者已是运行态，抄过来就污染了基线。

    Args:
        platform_type: 适配器类型（如 ``telegram``）。

    Returns:
        该类型的默认分组配置；DEFAULT_CONFIG 里没有该类型时返回空字典。
    """
    base = DEFAULT_CONFIG.get(PLATFORM_SPECIFIC_KEY)
    if not isinstance(base, dict):
        return {}
    group = base.get(platform_type)
    if not isinstance(group, dict):
        return {}
    return json.loads(json.dumps(group))


def _overlay_fields(profile: dict) -> dict[str, Any]:
    """从档案里筛出允许合入 platform 条目的字段。

    Args:
        profile: 已解析的档案字典。

    Returns:
        仅含白名单字段的子字典（顺序按 :data:`PLATFORM_OVERLAY_FIELDS`）。
    """
    return {
        key: profile[key]
        for key in PLATFORM_OVERLAY_FIELDS
        if key in profile and profile[key] is not None
    }


def _deep_merge(target: dict, overlay: dict) -> list[str]:
    """把 ``overlay`` 深度合并进 ``target``（就地修改）。

    Args:
        target: 被合并的字典（如 ``config["platform_specific"]["telegram"]``）。
        overlay: 覆盖值。

    Returns:
        实际发生变化的分组路径列表，用于幂等判定与日志。
    """
    changed: list[str] = []
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            changed.extend(
                f"{key}.{item}" for item in _deep_merge(target[key], value)
            )
            continue
        if key not in target or target[key] != value:
            target[key] = value
            changed.append(key)
    return changed


def merge_profile_into_entry(entry: dict, profile: dict) -> list[str]:
    """把一份档案的白名单字段合并进单个 platform 条目。

    只做**白名单字段级**合并：档案里没有的键不动，档案里出现了白名单外的键直接忽略。
    因此 token / appid / cookie 这类凭据永远安全。

    Args:
        entry: ``config["platform"][i]`` 条目（就地修改）。
        profile: 已解析的档案字典。

    Returns:
        被修改的字段名列表；空列表表示条目已是目标状态。
    """
    changed: list[str] = []
    for key, value in _overlay_fields(profile).items():
        if key not in entry or entry[key] != value:
            entry[key] = value
            changed.append(key)
    return changed


def merge_profile_into_platform_specific(
    config: dict,
    profile: dict,
    platform_type: str,
) -> list[str]:
    """把档案的 ``platform_specific`` 覆盖段合并进全局配置。

    档案里的 ``platform_specific`` 是 ``{分组名: 值}``；分组名通常就是适配器类型
    （与 ``DEFAULT_CONFIG["platform_specific"]`` 的键一致）。分组不存在时新建，
    已存在的分组做深度合并，不会清掉同组内档案未提及的键。

    Args:
        config: 全局配置字典（就地修改）。
        profile: 已解析的档案字典。
        platform_type: 对应条目声明的适配器类型，仅用于兜底分组名。

    Returns:
        实际发生变化的字段路径列表。
    """
    section = profile.get(PLATFORM_SPECIFIC_KEY)
    if not isinstance(section, dict) or not section:
        return []
    root = config.get("platform_specific")
    if not isinstance(root, dict):
        root = {}
        config["platform_specific"] = root
    changed: list[str] = []
    for group, values in section.items():
        group_name = str(group).strip()
        if not group_name or not isinstance(values, dict):
            continue
        bucket = root.get(group_name)
        if not isinstance(bucket, dict):
            bucket = {}
            root[group_name] = bucket
        changed.extend(
            f"{group_name}.{item}" for item in _deep_merge(bucket, values)
        )
    if changed and platform_type:
        logger.debug(
            "platform_specific 覆盖已更新（platform_type=%s）：%s",
            platform_type,
            changed,
        )
    return changed


def load_profile(path: str | Path) -> dict:
    """读取并校验单份档案文件。

    支持两种格式，按有无 ``_meta`` 自动判别：

    1. 原生格式：元数据在 ``_meta`` 段（:func:`build_profile` 写出的那种）；
    2. 契约格式：元数据与覆盖字段都在顶层（``adapter`` / ``platform_id`` /
       ``platform_type`` / ``cloned_from`` …），``tools/apply_adapter.py`` 落盘与
       ``adapters/<adapter>/profile.json`` 都是这种。

    契约格式读入后就地补齐 ``_meta``，使下游 :func:`apply_profiles` 无需分支；
    原生字段（``allowed_plugins`` 等）本来就与覆盖字段同名，无需搬动。

    Args:
        path: 档案文件路径。

    Returns:
        档案字典（一定含 ``_meta``）。

    Raises:
        ValueError: 文件不是合法 JSON 对象，或两种格式都取不到 ``platform_id``。
    """
    file_path = Path(path)
    with file_path.open(encoding="utf-8-sig") as handle:
        profile = json.load(handle)
    if not isinstance(profile, dict):
        raise ValueError(f"档案不是 JSON 对象：{file_path}")
    meta = profile.get(META_KEY)
    if not isinstance(meta, dict):
        # 契约格式：元数据在顶层，这里就地补齐 _meta，下游逻辑无需感知差异。
        platform_id = str(profile.get("platform_id") or "").strip()
        if not platform_id:
            raise ValueError(
                f"档案缺少 {META_KEY}，顶层也没有 platform_id：{file_path}",
            )
        meta = {
            "platform_id": platform_id,
            "platform_type": str(profile.get("platform_type") or "").strip(),
            "adapter": str(profile.get("adapter") or "").strip(),
            "cloned_from": profile.get("cloned_from"),
            "generated_at": profile.get("generated_at"),
            # 契约格式没有基线指纹：留空即不参与过期告警，避免每次启动刷警告。
            "default_config_fingerprint": "",
        }
        profile[META_KEY] = meta
    if not str(meta.get("platform_id") or "").strip():
        raise ValueError(f"档案缺少 {META_KEY}.platform_id：{file_path}")
    return profile


def _save_config(config: Any) -> bool:
    """尽力把配置写回磁盘。

    Args:
        config: ``AstrBotConfig`` 实例或普通 dict。

    Returns:
        ``True`` 表示确实落盘（调用方有 ``save_config``）；普通 dict 返回 ``False``。
    """
    saver = getattr(config, "save_config", None)
    if callable(saver):
        saver()
        return True
    return False


def apply_profiles(
    config: dict,
    *,
    profile_dir: str | Path | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """把档案目录下所有档案应用到给定配置。

    匹配规则：按 ``_meta.platform_id`` 找 ``config["platform"]`` 里 ``id`` 相同的条目。
    找不到对应条目时记错误并跳过，**绝不**新建条目（静默新建会让配置出现
    运行态里不存在的机器人）。档案的 ``platform_type`` 与条目 ``type`` 不一致时
    同样跳过，防止改名/改类型的条目被套上错误档案。

    Args:
        config: 全局配置（``AstrBotConfig`` 或普通 dict，就地修改）。
        profile_dir: 档案目录，缺省 ``<data>/config_platforms``。
        save: 有变化时是否写回磁盘（普通 dict 会忽略）。

    Returns:
        汇总字典，键：``ok`` / ``profile_dir`` / ``applied`` / ``unchanged`` /
        ``skipped`` / ``errors`` / ``warnings`` / ``saved``。
    """
    directory = get_profile_dir(profile_dir)
    summary: dict[str, Any] = {
        "ok": True,
        "profile_dir": str(directory),
        "applied": [],
        "unchanged": [],
        "skipped": [],
        "errors": [],
        "warnings": [],
        "saved": False,
    }

    platforms = config.get("platform")
    if not isinstance(platforms, list):
        summary["errors"].append(
            {"platform_id": None, "reason": "config['platform'] 不是列表，跳过"},
        )
        summary["ok"] = False
        return summary
    entries: dict[str, dict] = {}
    for item in platforms:
        if isinstance(item, dict):
            pid = str(item.get("id") or "").strip()
            if pid:
                entries[pid] = item

    if not directory.is_dir():
        summary["skipped"].append(
            {"platform_id": None, "reason": f"档案目录不存在：{directory}"},
        )
        return summary

    current_fingerprint = default_config_fingerprint()
    changed_any = False
    for path in sorted(directory.glob("*.json")):
        try:
            profile = load_profile(path)
        except Exception as exc:
            summary["errors"].append(
                {"platform_id": path.stem, "reason": f"档案无法读取：{exc}"},
            )
            summary["ok"] = False
            continue

        meta = profile[META_KEY]
        platform_id = str(meta.get("platform_id")).strip()
        profile_type = str(meta.get("platform_type") or "").strip()
        stored_fingerprint = str(meta.get("default_config_fingerprint") or "").strip()
        if stored_fingerprint and stored_fingerprint != current_fingerprint:
            summary["warnings"].append(
                {
                    "platform_id": platform_id,
                    "reason": (
                        "档案基线指纹与当前 DEFAULT_CONFIG 不一致，"
                        "档案可能派生自旧版本，建议重新生成"
                    ),
                },
            )

        entry = entries.get(platform_id)
        if entry is None:
            summary["errors"].append(
                {
                    "platform_id": platform_id,
                    "reason": "cmd_config 中找不到该 id 的机器人条目，跳过（不新建）",
                },
            )
            summary["ok"] = False
            continue
        entry_type = str(entry.get("type") or "").strip()
        if profile_type and entry_type and profile_type != entry_type:
            summary["errors"].append(
                {
                    "platform_id": platform_id,
                    "reason": (
                        f"适配器类型不一致（档案 {profile_type} / 配置 {entry_type}），跳过"
                    ),
                },
            )
            summary["ok"] = False
            continue

        changed = merge_profile_into_entry(entry, profile)
        changed.extend(
            merge_profile_into_platform_specific(config, profile, entry_type),
        )
        if changed:
            changed_any = True
            summary["applied"].append(
                {"platform_id": platform_id, "fields": changed},
            )
        else:
            summary["unchanged"].append(platform_id)

    if changed_any and save:
        summary["saved"] = _save_config(config)
    return summary


def apply_profiles_to_running_config(
    config: dict,
    *,
    profile_dir: str | Path | None = None,
) -> dict[str, Any]:
    """启动期入口：应用所有平台档案，任何异常都不外抛。

    AstrBot 启动流程调用本函数。档案目录缺失、档案损坏、字段类型异常都只记日志，
    启动继续 —— 配置档案是便利机制，不该成为启动的单点故障。

    Args:
        config: 全局配置（``AstrBotConfig`` 或普通 dict，就地修改）。
        profile_dir: 档案目录，缺省 ``<data>/config_platforms``。

    Returns:
        同 :func:`apply_profiles`；异常时返回 ``{"ok": False, "error": ...}``。
    """
    try:
        summary = apply_profiles(config, profile_dir=profile_dir)
    except Exception as exc:  # 兜底：绝不让档案机制影响启动
        logger.warning("[platform_profile] 应用平台配置档案失败，已忽略：%s", exc)
        return {"ok": False, "error": str(exc)}

    for item in summary.get("applied") or []:
        logger.info(
            "[platform_profile] 已应用档案 %s：%s",
            item["platform_id"],
            ", ".join(item["fields"]),
        )
    for item in summary.get("warnings") or []:
        logger.warning(
            "[platform_profile] %s：%s",
            item["platform_id"],
            item["reason"],
        )
    for item in summary.get("errors") or []:
        logger.warning(
            "[platform_profile] %s：%s",
            item.get("platform_id") or "-",
            item.get("reason"),
        )
    if not summary.get("applied") and not summary.get("errors"):
        logger.debug(
            "[platform_profile] 无档案需要应用（目录 %s）",
            summary.get("profile_dir"),
        )
    return summary


def build_profile(
    *,
    platform_id: str,
    platform_type: str,
    overlay: dict[str, Any],
    platform_specific: dict[str, dict] | None = None,
    note: str = "",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """构造一份档案字典（供生成脚本与测试使用）。

    Args:
        platform_id: 机器人实例 id（与 cmd_config 里 ``platform[i].id`` 一致）。
        platform_type: 适配器类型（如 ``qq_official``）。
        overlay: 白名单字段的覆盖值。
        platform_specific: ``platform_specific`` 覆盖段，键为分组名。
        note: 人类可读说明。
        generated_at: 生成时间字符串；``None`` 时取当前时间是调用方职责
            （本函数保持纯粹，不引入时间依赖便于测试）。

    Returns:
        可直接写入 ``<platform_id>.json`` 的字典。
    """
    from datetime import datetime

    overlay_clean = {
        key: value for key, value in overlay.items() if key in PLATFORM_OVERLAY_FIELDS
    }
    profile: dict[str, Any] = {
        META_KEY: {
            "schema_version": SCHEMA_VERSION,
            "source": "astrbot.core.config.default:DEFAULT_CONFIG",
            "default_config_fingerprint": default_config_fingerprint(),
            "platform_id": platform_id,
            "platform_type": platform_type,
            "generated_at": generated_at
            or datetime.now().astimezone().isoformat(timespec="seconds"),
            "note": note,
        },
    }
    profile.update(overlay_clean)
    if platform_specific:
        profile[PLATFORM_SPECIFIC_KEY] = platform_specific
    return profile


def write_profile(profile: dict, *, profile_dir: str | Path | None = None) -> Path:
    """把档案写到档案目录（文件名取 ``_meta.platform_id``）。

    Args:
        profile: :func:`build_profile` 的结果。
        profile_dir: 档案目录，缺省 ``<data>/config_platforms``。

    Returns:
        写出的文件路径。

    Raises:
        ValueError: 档案缺少 ``_meta.platform_id``。
    """
    meta = profile.get(META_KEY)
    platform_id = str((meta or {}).get("platform_id") or "").strip()
    if not platform_id:
        raise ValueError("档案缺少 _meta.platform_id，无法确定文件名")
    directory = get_profile_dir(profile_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{platform_id}.json"
    path.write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path
