"""OpenAI 兼容 API 服务（内核原生实现）。

把 OpenAI 协议请求映射到 AstrBot 既有能力，使 AstrBot 在 Dashboard 服务
（默认 6185 端口）上原生对外提供 OpenAI 兼容接口，而不仅是管理型 HTTP API。

端点映射
--------
- ``/v1/chat/completions``：走 AstrBot 完整消息管道（人格 / 插件工具 / 记忆），
  复用 webchat 注入通道（``OpenApiService.handle_chat_ws_send``）。
- ``/v1/completions``：同上，按 legacy 文本补全协议包装。
- ``/v1/embeddings``：EMBEDDING provider 直连。
- ``/v1/rerank``：RERANK provider 直连（Jina / Cohere 风格，非 OpenAI 标准）。
- ``/v1/audio/transcriptions`` / ``/v1/audio/translations``：SPEECH_TO_TEXT provider。
- ``/v1/audio/speech``：TEXT_TO_SPEECH provider。
- 其余端点（images / moderations / responses / files / batches / fine_tuning 等）
  注册为占位路由，统一返回标准 OpenAI 错误体，避免客户端探测时得到 404。

鉴权
----
独立于 Dashboard 的 API Key 体系：使用专用 Key（``Authorization: Bearer sk-...``）。
明文仅在建 Key 时返回一次；落盘内容为 SHA-256 摘要与前缀掩码。

会话
----
OpenAI 协议本身无会话概念。默认以「每个 Key 一个会话」维护记忆，可通过请求
``metadata.session_id`` 覆盖，从而由客户端自行控制对话隔离。

模型名
------
``model`` 字段使用 AstrBot 的 provider id；也可通过配置 ``model_aliases``
建立别名。未指定或无法解析时回落到 AstrBot 当前使用的 provider。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.core import logger
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
from astrbot.core.db import BaseDatabase
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.dashboard.services.chat_service import (
    ChatService,
    extract_web_search_refs,
)
from astrbot.dashboard.services.open_api_service import (
    OpenApiService,
    OpenApiWebSocketChatBridge,
)

CONFIG_FILENAME = "openai_compat.json"
KEY_PREFIX = "sk-astrbot-"
DEFAULT_USERNAME = "openai"

# HTTP 入口标识，对应 plugin_category_manager 的入口策略键
ENTRY_OPENAI_COMPAT = "openai_compat"


class OpenAICompatError(Exception):
    """OpenAI 风格错误：携带 HTTP 状态码与 ``error.type`` / ``error.code``。"""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        err_type: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.err_type = err_type
        self.code = code
        self.param = param

    def to_body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.err_type,
                "param": self.param,
                "code": self.code,
            }
        }


class UnsupportedEndpointError(OpenAICompatError):
    """内核不具备对应后端能力的端点。"""

    def __init__(self, endpoint: str, reason: str) -> None:
        super().__init__(
            f"{endpoint} is not supported by AstrBot (no {reason} backend available)",
            status=501,
            err_type="invalid_request_error",
            code="endpoint_not_supported",
        )


@dataclass
class KeyRecord:
    """一条 OpenAI 专用 Key（持久化形态，不含明文）。"""

    id: str
    key_hash: str
    key_prefix: str
    remark: str = ""
    username: str = DEFAULT_USERNAME
    enabled: bool = True
    created_at: float = 0.0
    last_used_at: float | None = None
    request_count: int = 0
    allow_plugins: list[str] = field(default_factory=list)
    """在 HTTP 入口额外放行的插件名（突破互动性插件的默认限制）。"""

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key_prefix": self.key_prefix,
            "remark": self.remark,
            "username": self.username,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "request_count": self.request_count,
            "allow_plugins": list(self.allow_plugins),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyRecord:
        raw_allow = data.get("allow_plugins")
        allow_plugins = (
            [str(item) for item in raw_allow if str(item).strip()]
            if isinstance(raw_allow, list)
            else []
        )
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            key_hash=str(data.get("key_hash") or ""),
            key_prefix=str(data.get("key_prefix") or ""),
            remark=str(data.get("remark") or ""),
            username=str(data.get("username") or DEFAULT_USERNAME),
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at") or 0.0),
            last_used_at=data.get("last_used_at"),
            request_count=int(data.get("request_count") or 0),
            allow_plugins=allow_plugins,
        )


@dataclass
class ChatRunResult:
    """一次对话执行的汇总结果。"""

    text: str = ""
    reasoning: str = ""
    finish_reason: str = "stop"
    attachments: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    """OpenAI 格式的工具调用（仅直连 provider 路径会产生）。"""
    mode: str = "pipeline"
    """``pipeline`` = 走 AstrBot 完整管道；``direct`` = 直连 provider（保留客户端 tools）。"""


def _token_estimate(text: str) -> int:
    """粗略估算 token 数：中文按 1 字 ≈ 1 token，英文按 4 字符 ≈ 1 token。"""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii = len(text) - ascii_chars
    return int(non_ascii + ascii_chars / 4) + 1


class OpenAICompatService:
    """OpenAI 兼容协议的协议适配层。"""

    def __init__(
        self,
        db: BaseDatabase,
        core_lifecycle: AstrBotCoreLifecycle,
        open_api_service: OpenApiService,
        chat_service: ChatService,
    ) -> None:
        self.db = db
        self.core_lifecycle = core_lifecycle
        self.open_api_service = open_api_service
        self.chat_service = chat_service
        self._config_path = Path(get_astrbot_data_path()) / CONFIG_FILENAME
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float = 0.0

    # ------------------------------------------------------------------
    # 配置读写
    # ------------------------------------------------------------------

    @staticmethod
    def _default_config() -> dict[str, Any]:
        return {
            "enabled": True,
            "default_username": DEFAULT_USERNAME,
            "default_model": "",
            "request_timeout": 180,
            "model_aliases": {},
            "keys": [],
            # 直连 provider（带 tools）时是否注入 AstrBot 人格
            "inject_persona": True,
            # 强制指定人格 id；留空表示按虚拟会话自动解析
            "persona_id": "",
            # 带 tools 的请求是否走完整管道（插件工具 + 客户端工具并存，
            # 客户端工具被拦截后回传客户端执行）。默认关闭，保持既有行为。
            "pipe_tools": False,
        }

    def _load(self, *, force: bool = False) -> dict[str, Any]:
        """读取配置；文件被外部修改时自动热加载。"""
        try:
            mtime = self._config_path.stat().st_mtime
        except OSError:
            mtime = 0.0

        if (
            not force
            and self._cache is not None
            and mtime == self._cache_mtime
        ):
            return self._cache

        config = self._default_config()
        if mtime:
            try:
                raw = json.loads(self._config_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    config.update(raw)
            except Exception as exc:
                logger.error(
                    "读取 OpenAI 兼容配置失败（%s）：%s", self._config_path, exc
                )

        self._cache = config
        self._cache_mtime = mtime
        return config

    def _save(self, config: dict[str, Any]) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._config_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self._config_path)
        self._cache = config
        try:
            self._cache_mtime = self._config_path.stat().st_mtime
        except OSError:
            self._cache_mtime = 0.0

    def is_enabled(self) -> bool:
        return bool(self._load().get("enabled", True))

    def public_config(self) -> dict[str, Any]:
        config = self._load()
        return {
            "enabled": bool(config.get("enabled", True)),
            "default_username": config.get("default_username", DEFAULT_USERNAME),
            "default_model": config.get("default_model", ""),
            "request_timeout": int(config.get("request_timeout", 180)),
            "model_aliases": dict(config.get("model_aliases") or {}),
            "inject_persona": bool(config.get("inject_persona", True)),
            "persona_id": config.get("persona_id", ""),
            "pipe_tools": bool(config.get("pipe_tools", False)),
            "base_path": "/v1",
        }

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        config = self._load(force=True)
        for key in (
            "enabled",
            "default_username",
            "default_model",
            "request_timeout",
            "inject_persona",
            "persona_id",
            "pipe_tools",
        ):
            if key in patch and patch[key] is not None:
                config[key] = patch[key]
        if isinstance(patch.get("model_aliases"), dict):
            config["model_aliases"] = {
                str(k): str(v)
                for k, v in patch["model_aliases"].items()
                if str(k).strip() and str(v).strip()
            }
        self._save(config)
        return self.public_config()

    # ------------------------------------------------------------------
    # Key 管理
    # ------------------------------------------------------------------

    @staticmethod
    def hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    @staticmethod
    def _mask_key(raw_key: str) -> str:
        if len(raw_key) <= 12:
            return raw_key[:4] + "****"
        return f"{raw_key[:12]}****{raw_key[-4:]}"

    def _keys(self) -> list[KeyRecord]:
        return [
            KeyRecord.from_dict(item)
            for item in (self._load().get("keys") or [])
            if isinstance(item, dict)
        ]

    def list_keys(self) -> list[dict[str, Any]]:
        return [record.to_public() for record in self._keys()]

    def create_key(
        self,
        remark: str = "",
        username: str | None = None,
        allow_plugins: list[str] | None = None,
    ) -> dict[str, Any]:
        """新建一条 Key，返回含一次性明文的记录。"""
        config = self._load(force=True)
        raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
        record = KeyRecord(
            id=str(uuid.uuid4()),
            key_hash=self.hash_key(raw_key),
            key_prefix=self._mask_key(raw_key),
            remark=str(remark or "").strip() or "未命名",
            username=str(username or config.get("default_username") or DEFAULT_USERNAME),
            enabled=True,
            created_at=time.time(),
            allow_plugins=[
                str(item) for item in (allow_plugins or []) if str(item).strip()
            ],
        )
        config.setdefault("keys", []).append(record.__dict__)
        self._save(config)
        public = record.to_public()
        public["key"] = raw_key  # 仅此一次返回明文
        return public

    def delete_key(self, key_id: str) -> bool:
        config = self._load(force=True)
        keys = [item for item in (config.get("keys") or []) if isinstance(item, dict)]
        remaining = [item for item in keys if str(item.get("id")) != str(key_id)]
        if len(remaining) == len(keys):
            return False
        config["keys"] = remaining
        self._save(config)
        return True

    def set_key_enabled(self, key_id: str, enabled: bool) -> bool:
        config = self._load(force=True)
        hit = False
        for item in config.get("keys") or []:
            if isinstance(item, dict) and str(item.get("id")) == str(key_id):
                item["enabled"] = bool(enabled)
                hit = True
        if not hit:
            return False
        self._save(config)
        return True

    def set_key_allow_plugins(self, key_id: str, plugins: list[str]) -> bool:
        """设置该 Key 在 HTTP 入口额外放行的插件（突破互动性默认限制）。"""
        config = self._load(force=True)
        cleaned = [str(item).strip() for item in (plugins or []) if str(item).strip()]
        hit = False
        for item in config.get("keys") or []:
            if isinstance(item, dict) and str(item.get("id")) == str(key_id):
                item["allow_plugins"] = cleaned
                hit = True
        if not hit:
            return False
        self._save(config)
        return True

    def verify_key(self, raw_key: str | None) -> KeyRecord:
        """校验专用 Key；失败抛 ``OpenAICompatError(401)``。"""
        if not raw_key:
            raise OpenAICompatError(
                "Missing API key. Provide it via the Authorization header.",
                status=401,
                err_type="invalid_request_error",
                code="invalid_api_key",
            )
        key_hash = self.hash_key(raw_key)
        for record in self._keys():
            if secrets.compare_digest(record.key_hash, key_hash):
                if not record.enabled:
                    raise OpenAICompatError(
                        "This API key has been disabled.",
                        status=401,
                        err_type="invalid_request_error",
                        code="invalid_api_key",
                    )
                self._touch_key(record.id)
                return record
        raise OpenAICompatError(
            "Incorrect API key provided.",
            status=401,
            err_type="invalid_request_error",
            code="invalid_api_key",
        )

    def _touch_key(self, key_id: str) -> None:
        config = self._load()
        changed = False
        for item in config.get("keys") or []:
            if isinstance(item, dict) and str(item.get("id")) == str(key_id):
                item["last_used_at"] = time.time()
                item["request_count"] = int(item.get("request_count") or 0) + 1
                changed = True
                break
        if changed:
            try:
                self._save(config)
            except Exception as exc:  # 统计失败不影响请求
                logger.debug("更新 OpenAI Key 统计失败：%s", exc)

    # ------------------------------------------------------------------
    # 模型
    # ------------------------------------------------------------------

    def _provider_manager(self):
        return self.core_lifecycle.provider_manager

    def _chat_providers(self) -> list[Any]:
        return list(getattr(self._provider_manager(), "provider_insts", []) or [])

    def list_models(self) -> list[dict[str, Any]]:
        """OpenAI ``/v1/models`` 列表：provider 实例 + 别名。"""
        created = int(time.time())
        models: list[dict[str, Any]] = []
        seen: set[str] = set()

        for inst in self._chat_providers():
            try:
                meta = inst.meta()
            except Exception:
                continue
            model_id = str(meta.id)
            seen.add(model_id)
            models.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": created,
                    "owned_by": "astrbot",
                }
            )

        for alias, target in (self._load().get("model_aliases") or {}).items():
            alias = str(alias)
            if alias in seen:
                continue
            seen.add(alias)
            models.append(
                {
                    "id": alias,
                    "object": "model",
                    "created": created,
                    "owned_by": f"astrbot:{target}",
                }
            )
        return models

    def resolve_model(self, model: str | None) -> str | None:
        """把请求中的 ``model`` 解析为 AstrBot provider id。

        Returns:
            解析出的 provider id；无法解析或被显式置空时返回 ``None``
            （交由 AstrBot 使用当前默认 provider）。
        """
        config = self._load()
        aliases = config.get("model_aliases") or {}
        candidate = str(model).strip() if model else ""
        if candidate and candidate in aliases:
            candidate = str(aliases[candidate]).strip()
        if not candidate:
            candidate = str(config.get("default_model") or "").strip()
        if not candidate:
            return None
        available = set()
        for inst in self._chat_providers():
            try:
                available.add(str(inst.meta().id))
            except Exception:
                continue
        if candidate in available:
            return candidate
        raise OpenAICompatError(
            f"The model `{candidate}` does not exist or is not a chat provider.",
            status=404,
            err_type="invalid_request_error",
            code="model_not_found",
            param="model",
        )

    # ------------------------------------------------------------------
    # 对话（走完整管道）
    # ------------------------------------------------------------------

    def _build_chat_bridge(self) -> OpenApiWebSocketChatBridge:
        return OpenApiWebSocketChatBridge(
            build_user_message_parts=lambda message: self.chat_service.build_user_message_parts(
                message if isinstance(message, (str, list)) else str(message),
            ),
            create_attachment_from_file=self.chat_service.create_attachment_from_file,
            extract_web_search_refs=extract_web_search_refs,
            insert_user_message=lambda session_id, effective_username, message_parts: (
                self.open_api_service.insert_webchat_user_message(
                    session_id=session_id,
                    effective_username=effective_username,
                    message_parts=message_parts,
                )
            ),
            save_bot_message=self.chat_service.save_bot_message,
        )

    @staticmethod
    def extract_prompt(messages: Any) -> str:
        """从 OpenAI ``messages`` 中取出最后一条 user 消息的文本。"""
        if not isinstance(messages, list):
            raise OpenAICompatError(
                "`messages` must be an array.",
                param="messages",
            )
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if str(message.get("role")) != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                if content.strip():
                    return content
            elif isinstance(content, list):
                texts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        texts.append(str(part.get("text") or ""))
                    elif isinstance(part, str):
                        texts.append(part)
                joined = "".join(texts).strip()
                if joined:
                    return joined
        raise OpenAICompatError(
            "No user message with text content found.",
            param="messages",
            code="invalid_messages",
        )

    async def run_chat(
        self,
        body: dict[str, Any],
        key: KeyRecord,
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ChatRunResult:
        """执行一次对话补全。

        路径选择：

        - 续轮（``messages`` 已含 ``tool`` 结果）→ **直连 provider**：完整历史由
          客户端提供，避免与管道记忆重复累积。
        - 首轮携带 ``tools`` 且开启 ``pipe_tools`` → 走 AstrBot **完整管道**：
          AstrBot 插件工具与客户端工具并存，外部工具调用被拦截后回传客户端执行。
        - 首轮携带 ``tools`` 且未开启 ``pipe_tools`` → **直连 provider**：
          完整保留客户端工具定义与多轮 ``tool`` 消息（标准 function calling 回环）。
        - 未携带 ``tools`` → 走 AstrBot **完整管道**（人格 / 插件工具 / 记忆）。

        Args:
            body: OpenAI 协议请求体。
            key: 已鉴权的 Key 记录。
            on_delta: 流式增量回调。

        Returns:
            汇总结果，含完整文本、工具调用与结束原因。
        """
        # 客户端工具回环的续轮：交给直连，上下文以客户端 messages 为准
        if self._is_tool_continuation(body):
            return await self._run_chat_direct(body, key, on_delta=on_delta)

        if body.get("tools"):
            if bool(self._load().get("pipe_tools", False)):
                # 管道模式：插件工具与客户端工具并存，外部工具被拦截回传
                return await self._run_chat_pipeline(body, key, on_delta=on_delta)
            return await self._run_chat_direct(body, key, on_delta=on_delta)

        return await self._run_chat_pipeline(body, key, on_delta=on_delta)

    @staticmethod
    def _is_tool_continuation(body: dict[str, Any]) -> bool:
        """是否为客户端工具回环的续轮（``messages`` 中已含 ``tool`` 结果）。"""
        messages = body.get("messages")
        if not isinstance(messages, list):
            return False
        return any(
            isinstance(item, dict) and str(item.get("role")) == "tool"
            for item in messages
        )

    async def _run_chat_pipeline(
        self,
        body: dict[str, Any],
        key: KeyRecord,
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ChatRunResult:
        """走 AstrBot 完整管道（人格 + 插件工具 + 记忆），复用 webchat 注入通道。"""
        config = self._load()
        prompt = self.extract_prompt(body.get("messages"))
        provider_id = self.resolve_model(body.get("model"))
        username, session_id = self._derive_identity(body, key)

        timeout = int(config.get("request_timeout") or 180)
        bridge = self._build_chat_bridge()
        post_data: dict[str, Any] = {
            "username": username,
            "session_id": session_id,
            "message": prompt,
            "selected_provider": provider_id,
            "message_id": str(uuid.uuid4()),
            # 入口标识：让插件类别策略识别这是 HTTP 入口（默认只放功能性插件）
            "_entry": ENTRY_OPENAI_COMPAT,
            # 按 Key 额外放行的插件（突破互动性插件在该入口的默认限制）
            "_extra_allowed_plugins": list(key.allow_plugins),
            # 客户端工具（外部工具）：管道会声明给模型，被调用时拦截回传客户端
            "_client_tools": body.get("tools") or [],
        }

        result = ChatRunResult()
        errors: list[OpenAICompatError] = []
        counter = 0

        async def send_json(payload: dict[str, Any]) -> None:
            nonlocal counter
            msg_type = payload.get("type")
            streaming = bool(payload.get("streaming"))
            chain_type = payload.get("chain_type")
            data = payload.get("data") or ""

            # 外部工具（客户端执行）：管道已中断本轮，这里收集待回传的调用
            if chain_type == "external_tool_calls" and isinstance(data, str):
                try:
                    parsed = json.loads(data)
                    calls = parsed.get("calls") if isinstance(parsed, dict) else None
                    if isinstance(calls, list):
                        result.tool_calls = [
                            {
                                "id": str(call.get("id") or f"call_{index}"),
                                "type": "function",
                                "function": {
                                    "name": str(call.get("name") or ""),
                                    "arguments": (
                                        call.get("arguments")
                                        if isinstance(call.get("arguments"), str)
                                        else json.dumps(
                                            call.get("arguments") or {},
                                            ensure_ascii=False,
                                        )
                                    ),
                                },
                            }
                            for index, call in enumerate(calls)
                            if isinstance(call, dict)
                        ]
                        result.finish_reason = "tool_calls"
                except Exception as exc:
                    logger.warning("解析外部工具调用失败：%s", exc)
                return

            if msg_type == "plain" and isinstance(data, str):
                if chain_type == "reasoning":
                    result.reasoning += data
                    return
                if chain_type in ("tool_call", "tool_call_result"):
                    return
                if streaming:
                    result.text += data
                    counter += 1
                    if on_delta is not None:
                        await on_delta(data)
                else:
                    # 非流式分支：管道给出的即为完整文本
                    result.text = data
            elif msg_type in ("image", "record", "file", "video"):
                result.attachments.append({"type": msg_type, "data": data})

        async def send_error(message: str, code: str) -> None:
            errors.append(
                OpenAICompatError(
                    message,
                    status=502,
                    err_type="api_error",
                    code=code,
                )
            )

        try:
            await asyncio.wait_for(
                self.open_api_service.handle_chat_ws_send(
                    post_data=post_data,
                    conf_list=self.open_api_service.get_chat_config_list(),
                    chat_bridge=bridge,
                    send_json=send_json,
                    send_error=send_error,
                    allow_admin_username=True,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise OpenAICompatError(
                f"Request timed out after {timeout}s.",
                status=504,
                err_type="api_error",
                code="timeout",
            ) from exc
        except OpenAICompatError:
            raise
        except Exception as exc:
            logger.exception("OpenAI 兼容对话失败：%s", exc, exc_info=True)
            raise OpenAICompatError(
                f"Failed to run chat pipeline: {exc}",
                status=500,
                err_type="api_error",
                code="internal_error",
            ) from exc

        if errors:
            raise errors[0]

        if not result.text.strip() and not result.tool_calls:
            logger.warning(
                "OpenAI 兼容对话未产生文本（provider=%s, session=%s）",
                provider_id,
                session_id,
            )
            raise OpenAICompatError(
                "The model returned an empty response.",
                status=502,
                err_type="api_error",
                code="empty_response",
            )

        return result

    # ------------------------------------------------------------------
    # 会话身份与人格（管道 / 直连两条路径共用）
    # ------------------------------------------------------------------

    def _derive_identity(self, body: dict[str, Any], key: KeyRecord) -> tuple[str, str]:
        """派生会话身份 ``(username, session_id)``。

        两条路径共用同一套派生规则，保证人格解析与记忆落在同一会话上。
        """
        config = self._load()
        metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        username = str(
            metadata.get("username") or key.username or config.get("default_username")
        ).strip() or DEFAULT_USERNAME
        conversation_id = str(
            metadata.get("session_id") or body.get("user") or f"openai-{key.id}"
        ).strip()
        return username, conversation_id or f"openai-{key.id}"

    @staticmethod
    def _virtual_umo(username: str, session_id: str) -> str:
        """与管道路径一致的虚拟会话标识（人格 / 配置路由按此解析）。"""
        return f"webchat:FriendMessage:webchat!{username}!{session_id}"

    async def resolve_persona_prompt(self, umo: str) -> str | None:
        """解析该会话最终生效的人格提示词。

        Args:
            umo: 统一消息来源标识（虚拟会话）。

        Returns:
            人格提示词；未启用注入、没有人格或解析失败时返回 ``None``。
        """
        config = self._load()
        if not bool(config.get("inject_persona", True)):
            return None

        persona_mgr = getattr(self.core_lifecycle, "persona_mgr", None)
        if persona_mgr is None:
            return None

        forced_id = str(config.get("persona_id") or "").strip()
        persona: Any = None
        try:
            if forced_id:
                persona = persona_mgr.get_persona_v3_by_id(forced_id)
                if persona is None:
                    logger.warning(
                        "配置的人格 id `%s` 不存在，回落到自动解析", forced_id
                    )
            if persona is None:
                _, persona, _, _ = await persona_mgr.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=None,
                    platform_name="webchat",
                    provider_settings=self.core_lifecycle.astrbot_config,
                )
        except Exception as exc:
            logger.warning("直连路径解析人格失败：%s", exc)
            return None

        if not persona:
            return None
        if isinstance(persona, dict):
            prompt = str(persona.get("prompt") or "")
        else:
            prompt = str(getattr(persona, "prompt", "") or "")
        return prompt.strip() or None

    @staticmethod
    def _apply_persona_to_contexts(
        contexts: list[dict[str, Any]],
        persona_prompt: str | None,
    ) -> None:
        """把人格提示词并入 contexts（追加到既有 system，没有则新建）。"""
        if not persona_prompt:
            return
        block = f"# Persona Instructions\n\n{persona_prompt}"
        for item in contexts:
            if item.get("role") != "system":
                continue
            existing = item.get("content")
            if isinstance(existing, str) and existing.strip():
                item["content"] = f"{existing}\n\n{block}"
            else:
                item["content"] = block
            return
        contexts.insert(0, {"role": "system", "content": block})

    # ------------------------------------------------------------------
    # 对话（直连 provider —— 完整保留客户端 tools）
    # ------------------------------------------------------------------

    def _resolve_chat_provider(self, model: str | None) -> Any:
        """按 ``model`` 解析用于直连的 provider 实例。"""
        provider_id = self.resolve_model(model)
        insts = self._chat_providers()
        if provider_id:
            for inst in insts:
                try:
                    if str(inst.meta().id) == provider_id:
                        return inst
                except Exception:
                    continue
        current = getattr(self._provider_manager(), "curr_provider_inst", None)
        if current is not None:
            return current
        if insts:
            return insts[0]
        raise OpenAICompatError(
            "No chat provider is configured.",
            status=503,
            err_type="api_error",
            code="provider_not_configured",
        )

    @staticmethod
    def _normalize_direct_messages(messages: Any) -> list[dict[str, Any]]:
        """规整客户端 messages，保留 ``tool`` 角色与 ``tool_calls`` 语义。"""
        if not isinstance(messages, list) or not messages:
            raise OpenAICompatError(
                "`messages` must be a non-empty array.", param="messages"
            )
        allowed_roles = {"system", "user", "assistant", "tool", "developer"}
        normalized: list[dict[str, Any]] = []
        for raw in messages:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "").strip()
            if role not in allowed_roles:
                continue
            item: dict[str, Any] = {
                "role": "system" if role == "developer" else role,
            }
            if "content" in raw:
                item["content"] = raw.get("content")
            if raw.get("tool_calls"):
                item["tool_calls"] = raw["tool_calls"]
            if raw.get("tool_call_id"):
                item["tool_call_id"] = raw["tool_call_id"]
            if raw.get("name"):
                item["name"] = raw["name"]
            normalized.append(item)
        if not normalized:
            raise OpenAICompatError(
                "No valid message with a known role was provided.",
                param="messages",
            )
        return normalized

    @staticmethod
    def _build_client_toolset(tools: Any) -> Any | None:
        """把客户端的 OpenAI tools 定义转成 AstrBot ``ToolSet``。

        委托内核 ``external_tools.build_toolset``，保证「直连路径」与「管道路径」
        对工具定义的解析完全一致。工具**不绑定执行者** —— 由客户端执行后回传。
        """
        if not isinstance(tools, list) or not tools:
            return None
        try:
            from astrbot.core.agent.external_tools import build_toolset
        except Exception as exc:
            raise OpenAICompatError(
                f"Tool support is unavailable: {exc}",
                status=500,
                err_type="api_error",
                code="tool_support_unavailable",
            ) from exc
        return build_toolset(tools)

    @staticmethod
    def _to_openai_tool_calls(response: Any) -> list[dict[str, Any]]:
        """把 ``LLMResponse`` 的工具调用转回 OpenAI 格式。"""
        names = list(getattr(response, "tools_call_name", None) or [])
        args = list(getattr(response, "tools_call_args", None) or [])
        ids = list(getattr(response, "tools_call_ids", None) or [])
        extras = getattr(response, "tools_call_extra_content", None) or {}

        calls: list[dict[str, Any]] = []
        for index, name in enumerate(names):
            call_id = (
                str(ids[index]) if index < len(ids) else f"call_{uuid.uuid4().hex}"
            )
            raw_args = args[index] if index < len(args) else {}
            arguments = (
                raw_args
                if isinstance(raw_args, str)
                else json.dumps(raw_args, ensure_ascii=False)
            )
            entry: dict[str, Any] = {
                "id": call_id,
                "type": "function",
                "function": {"name": str(name), "arguments": arguments},
            }
            extra = extras.get(call_id) if isinstance(extras, dict) else None
            if extra:
                entry["extra_content"] = extra
            calls.append(entry)
        return calls

    async def _run_chat_direct(
        self,
        body: dict[str, Any],
        key: KeyRecord,
        *,
        on_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> ChatRunResult:
        """直连 provider：完整保留客户端 ``tools``，并注入 AstrBot 人格。"""
        provider = self._resolve_chat_provider(body.get("model"))
        contexts = self._normalize_direct_messages(body.get("messages"))

        # 注入人格：按虚拟会话解析，使外部客户端同样获得 AstrBot 人格与世界观
        username, session_id = self._derive_identity(body, key)
        persona_prompt = await self.resolve_persona_prompt(
            self._virtual_umo(username, session_id)
        )
        self._apply_persona_to_contexts(contexts, persona_prompt)

        toolset = self._build_client_toolset(body.get("tools"))

        kwargs: dict[str, Any] = {"contexts": contexts, "func_tool": toolset}
        tool_choice = body.get("tool_choice")
        if tool_choice in ("auto", "required"):
            kwargs["tool_choice"] = tool_choice
        for passthrough in ("temperature", "top_p", "max_tokens", "stop"):
            if body.get(passthrough) is not None:
                kwargs[passthrough] = body[passthrough]

        try:
            response = await provider.text_chat(**kwargs)
        except OpenAICompatError:
            raise
        except Exception as exc:
            logger.exception("直连 provider 调用失败：%s", exc, exc_info=True)
            raise OpenAICompatError(
                f"Upstream provider failed: {exc}",
                status=502,
                err_type="api_error",
                code="upstream_error",
            ) from exc

        result = ChatRunResult(mode="direct")
        result.text = str(getattr(response, "completion_text", "") or "")
        result.tool_calls = self._to_openai_tool_calls(response)
        if result.tool_calls:
            result.finish_reason = "tool_calls"
        if on_delta is not None and result.text:
            # 直连路径整体是一次性收发，这里把文本作为单个增量交付，
            # 保证流式客户端仍能拿到内容。
            await on_delta(result.text)
        return result

    # ------------------------------------------------------------------
    # 向量 / 重排
    # ------------------------------------------------------------------

    def _pick_provider(self, insts: list[Any], model: str | None) -> Any:
        if not insts:
            raise OpenAICompatError(
                "No provider of the required type is configured.",
                status=501,
                err_type="invalid_request_error",
                code="provider_not_configured",
            )
        candidate = str(model).strip() if model else ""
        if candidate:
            for inst in insts:
                try:
                    if str(inst.meta().id) == candidate:
                        return inst
                except Exception:
                    continue
            raise OpenAICompatError(
                f"The model `{candidate}` does not exist for this endpoint.",
                status=404,
                err_type="invalid_request_error",
                code="model_not_found",
                param="model",
            )
        return insts[0]

    @staticmethod
    def _normalize_input(text: Any) -> list[str]:
        if isinstance(text, str):
            return [text]
        if isinstance(text, list):
            items = [str(item) for item in text if isinstance(item, (str, int, float))]
            if items:
                return items
        raise OpenAICompatError(
            "`input` must be a string or an array of strings.",
            param="input",
        )

    async def embeddings(self, body: dict[str, Any]) -> dict[str, Any]:
        texts = self._normalize_input(body.get("input"))
        insts = list(
            getattr(self._provider_manager(), "embedding_provider_insts", []) or []
        )
        provider = self._pick_provider(insts, body.get("model"))

        try:
            vectors = await provider.get_embeddings(texts)
        except OpenAICompatError:
            raise
        except Exception as exc:
            logger.exception("Embedding 调用失败：%s", exc, exc_info=True)
            raise OpenAICompatError(
                f"Embedding backend failed: {exc}",
                status=502,
                err_type="api_error",
                code="embedding_failed",
            ) from exc

        data = [
            {"object": "embedding", "index": index, "embedding": list(vector)}
            for index, vector in enumerate(vectors or [])
        ]
        prompt_tokens = sum(_token_estimate(text) for text in texts)
        return {
            "object": "list",
            "data": data,
            "model": body.get("model") or getattr(provider.meta(), "id", "astrbot"),
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }

    async def rerank(self, body: dict[str, Any]) -> dict[str, Any]:
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            raise OpenAICompatError("`query` is required.", param="query")
        documents = body.get("documents")
        if not isinstance(documents, list) or not documents:
            raise OpenAICompatError(
                "`documents` must be a non-empty array.", param="documents"
            )
        docs = [doc if isinstance(doc, str) else json.dumps(doc, ensure_ascii=False) for doc in documents]
        top_n = body.get("top_n")
        top_n = int(top_n) if isinstance(top_n, (int, float, str)) and str(top_n).isdigit() else None

        insts = list(
            getattr(self._provider_manager(), "rerank_provider_insts", []) or []
        )
        provider = self._pick_provider(insts, body.get("model"))
        try:
            ranked = await provider.rerank(query, docs, top_n)
        except OpenAICompatError:
            raise
        except Exception as exc:
            logger.exception("Rerank 调用失败：%s", exc, exc_info=True)
            raise OpenAICompatError(
                f"Rerank backend failed: {exc}",
                status=502,
                err_type="api_error",
                code="rerank_failed",
            ) from exc

        results = []
        for item in ranked or []:
            index = int(getattr(item, "index", 0))
            results.append(
                {
                    "index": index,
                    "relevance_score": float(getattr(item, "relevance_score", 0.0)),
                    "document": {"text": docs[index] if 0 <= index < len(docs) else ""},
                }
            )
        return {
            "id": f"rerank-{uuid.uuid4().hex}",
            "results": results,
            "model": body.get("model") or getattr(provider.meta(), "id", "astrbot"),
            "usage": {"total_tokens": _token_estimate(query)},
        }

    # ------------------------------------------------------------------
    # 语音
    # ------------------------------------------------------------------

    async def transcribe(
        self,
        *,
        file_path: str,
        model: str | None = None,
        translate: bool = False,
    ) -> dict[str, Any]:
        manager = self._provider_manager()
        current = getattr(manager, "curr_stt_provider_inst", None)
        insts = list(getattr(manager, "stt_provider_insts", []) or [])
        provider = current if (current is not None and not model) else self._pick_provider(insts, model)

        try:
            text = await provider.get_text(file_path)
        except OpenAICompatError:
            raise
        except Exception as exc:
            logger.exception("语音识别失败：%s", exc, exc_info=True)
            raise OpenAICompatError(
                f"Speech-to-text backend failed: {exc}",
                status=502,
                err_type="api_error",
                code="transcription_failed",
            ) from exc

        return {"text": text or ""}

    async def speech(self, body: dict[str, Any]) -> tuple[bytes, str]:
        text = body.get("input")
        if not isinstance(text, str) or not text.strip():
            raise OpenAICompatError("`input` is required.", param="input")

        manager = self._provider_manager()
        current = getattr(manager, "curr_tts_provider_inst", None)
        insts = list(getattr(manager, "tts_provider_insts", []) or [])
        provider = current if (current is not None and not body.get("model")) else self._pick_provider(insts, body.get("model"))

        try:
            audio_path = await provider.get_audio(text)
        except OpenAICompatError:
            raise
        except Exception as exc:
            logger.exception("语音合成失败：%s", exc, exc_info=True)
            raise OpenAICompatError(
                f"Text-to-speech backend failed: {exc}",
                status=502,
                err_type="api_error",
                code="speech_failed",
            ) from exc

        if not audio_path or not os.path.exists(audio_path):
            raise OpenAICompatError(
                "Text-to-speech backend returned no audio file.",
                status=502,
                err_type="api_error",
                code="speech_failed",
            )
        with open(audio_path, "rb") as fp:
            payload = fp.read()

        suffix = Path(audio_path).suffix.lower()
        content_type = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".opus": "audio/opus",
            ".aac": "audio/aac",
            ".flac": "audio/flac",
        }.get(suffix, "audio/mpeg")
        return payload, content_type
