"""OpenAI 兼容 API 路由（``/v1/*``）。

与 Dashboard 管理面（``/api/v1/*``）并存，共用同一个 ASGI 应用与监听端口，
但使用**独立**的鉴权体系：本模块接受 ``Authorization: Bearer sk-...`` 形式的
OpenAI 专用 Key（Dashboard 管理面的 ``Bearer`` 语义为 JWT，二者互不干扰）。

另外在 ``/api/v1/openai-compat/*`` 暴露一组管理接口，用于在 Dashboard 侧
创建、停用、删除专用 Key，以及调整默认用户名 / 模型别名等配置。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from astrbot.dashboard.services.openai_compat_service import (
    OpenAICompatError,
    OpenAICompatService,
    UnsupportedEndpointError,
)

from .auth import require_dashboard_user

v1_router = APIRouter(prefix="/v1", tags=["OpenAI Compat"])
admin_router = APIRouter(
    prefix="/openai-compat",
    tags=["OpenAI Compat"],
    dependencies=[Depends(require_dashboard_user)],
)


def get_service(request: Request) -> OpenAICompatService:
    return request.app.state.services.openai_compat


def _extract_bearer(request: Request) -> str | None:
    """从 OpenAI 客户端常用位置提取 Key。"""
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.startswith("Bearer "):
        return auth_header.removeprefix("Bearer ").strip()
    if auth_header.startswith("ApiKey "):
        return auth_header.removeprefix("ApiKey ").strip()
    if key := request.headers.get("X-API-Key"):
        return key.strip()
    if key := request.query_params.get("api_key"):
        return key.strip()
    return None


async def require_openai_key(request: Request) -> Any:
    """OpenAI 入口鉴权依赖：返回已校验的 Key 记录。"""
    service = get_service(request)
    if not service.is_enabled():
        raise OpenAICompatError(
            "The OpenAI-compatible endpoint is disabled.",
            status=503,
            err_type="api_error",
            code="endpoint_disabled",
        )
    return service.verify_key(_extract_bearer(request))


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


@v1_router.get("/models")
async def list_models(
    request: Request,
    _key: Any = Depends(require_openai_key),
):
    return {"object": "list", "data": get_service(request).list_models()}


@v1_router.get("/models/{model_id:path}")
async def retrieve_model(
    model_id: str,
    request: Request,
    _key: Any = Depends(require_openai_key),
):
    for item in get_service(request).list_models():
        if item["id"] == model_id:
            return item
    raise OpenAICompatError(
        f"The model `{model_id}` does not exist.",
        status=404,
        code="model_not_found",
        param="model",
    )


# ---------------------------------------------------------------------------
# 对话
# ---------------------------------------------------------------------------


class ChatCompletionRequest(BaseModel):
    """OpenAI ``/v1/chat/completions`` 请求体（宽松模式，未知字段透传）。"""

    model_config = {"extra": "allow"}

    model: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None
    metadata: dict[str, Any] | None = None


def _chunk_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _chunk(
    chunk_id: str,
    created: int,
    model_name: str,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> str:
    return _sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }
    )


@v1_router.post("/chat/completions")
async def chat_completions(
    payload: ChatCompletionRequest,
    request: Request,
    key: Any = Depends(require_openai_key),
):
    service = get_service(request)
    body = payload.model_dump(exclude_none=True)
    model_name = body.get("model") or "astrbot"

    if not payload.stream:
        result = await service.run_chat(body, key)
        prompt_tokens = sum(
            len(str(message.get("content") or ""))
            for message in body.get("messages") or []
        )
        completion_tokens = len(result.text)
        message: dict[str, Any] = {"role": "assistant"}
        if result.tool_calls:
            # 按 OpenAI 规范：发起工具调用时 content 允许为 null
            message["content"] = result.text or None
            message["tool_calls"] = result.tool_calls
        else:
            message["content"] = result.text
        if result.reasoning:
            message["reasoning_content"] = result.reasoning
        return {
            "id": _chunk_id(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": result.finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    chunk_id = _chunk_id()
    created = int(time.time())
    queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()

    async def on_delta(text: str) -> None:
        await queue.put(("delta", text))

    async def runner() -> None:
        try:
            result = await service.run_chat(body, key, on_delta=on_delta)
            if result.tool_calls:
                await queue.put(("tool_calls", result.tool_calls))
            await queue.put(("finish", result.finish_reason))
        except OpenAICompatError as exc:
            await queue.put(("error", exc.to_body()["error"]))
        except Exception as exc:  # pragma: no cover - 兜底
            await queue.put(("error", {"message": str(exc), "type": "api_error"}))
        finally:
            await queue.put(None)

    async def event_stream():
        task = asyncio.create_task(runner())
        failed = False
        finish_reason = "stop"
        try:
            yield _chunk(chunk_id, created, model_name, {"role": "assistant"}, None)
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, data = item
                if kind == "error":
                    failed = True
                    yield _sse({"error": data})
                    break
                if kind == "delta":
                    yield _chunk(
                        chunk_id, created, model_name, {"content": data}, None
                    )
                elif kind == "tool_calls":
                    yield _chunk(
                        chunk_id,
                        created,
                        model_name,
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call.get("id"),
                                    "type": "function",
                                    "function": call.get("function"),
                                }
                                for index, call in enumerate(data or [])
                            ]
                        },
                        None,
                    )
                elif kind == "finish":
                    finish_reason = str(data or "stop")
            if not failed:
                yield _chunk(chunk_id, created, model_name, {}, finish_reason)
            yield "data: [DONE]\n\n"
        finally:
            await task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


class CompletionRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str | None = None
    prompt: str | list[str] | None = None
    stream: bool = False
    user: str | None = None
    metadata: dict[str, Any] | None = None


@v1_router.post("/completions")
async def completions(
    payload: CompletionRequest,
    request: Request,
    key: Any = Depends(require_openai_key),
):
    """Legacy 文本补全：包装为 chat 请求后走同一管道。"""
    prompt = payload.prompt
    if isinstance(prompt, list):
        prompt = "\n".join(str(item) for item in prompt)
    if not isinstance(prompt, str) or not prompt.strip():
        raise OpenAICompatError("`prompt` is required.", param="prompt")

    result = await get_service(request).run_chat(
        {
            "model": payload.model,
            "messages": [{"role": "user", "content": prompt}],
            "user": payload.user,
            "metadata": payload.metadata,
        },
        key,
    )
    return {
        "id": f"cmpl-{uuid.uuid4().hex}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": payload.model or "astrbot",
        "choices": [
            {
                "index": 0,
                "text": result.text,
                "finish_reason": result.finish_reason,
            }
        ],
    }


# ---------------------------------------------------------------------------
# 向量 / 重排
# ---------------------------------------------------------------------------


class EmbeddingRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str | None = None
    input: Any = None


@v1_router.post("/embeddings")
async def embeddings(
    payload: EmbeddingRequest,
    request: Request,
    _key: Any = Depends(require_openai_key),
):
    return await get_service(request).embeddings(payload.model_dump(exclude_none=True))


class RerankRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str | None = None
    query: str | None = None
    documents: list[Any] | None = None
    top_n: int | None = None


@v1_router.post("/rerank")
async def rerank(
    payload: RerankRequest,
    request: Request,
    _key: Any = Depends(require_openai_key),
):
    return await get_service(request).rerank(payload.model_dump(exclude_none=True))


# ---------------------------------------------------------------------------
# 语音
# ---------------------------------------------------------------------------


async def _save_upload(file: UploadFile) -> str:
    suffix = os.path.splitext(file.filename or "audio")[1] or ".wav"
    fd, path = tempfile.mkstemp(prefix="openai_compat_", suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as fp:
            while chunk := await file.read(1024 * 1024):
                fp.write(chunk)
    except Exception:
        os.unlink(path)
        raise
    return path


@v1_router.post("/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str | None = Form(None),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    _key: Any = Depends(require_openai_key),
):
    del language, prompt, response_format
    service = get_service(request)
    path = await _save_upload(file)
    try:
        return await service.transcribe(file_path=path, model=model)
    finally:
        _safe_unlink(path)


@v1_router.post("/audio/translations")
async def audio_translations(
    request: Request,
    file: UploadFile = File(...),
    model: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    _key: Any = Depends(require_openai_key),
):
    del prompt, response_format
    service = get_service(request)
    path = await _save_upload(file)
    try:
        return await service.transcribe(file_path=path, model=model, translate=True)
    finally:
        _safe_unlink(path)


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class SpeechRequest(BaseModel):
    model_config = {"extra": "allow"}

    model: str | None = None
    input: str | None = None
    voice: str | None = None
    response_format: str | None = None
    speed: float | None = None


@v1_router.post("/audio/speech")
async def audio_speech(
    payload: SpeechRequest,
    request: Request,
    _key: Any = Depends(require_openai_key),
):
    audio, content_type = await get_service(request).speech(
        payload.model_dump(exclude_none=True)
    )
    return Response(content=audio, media_type=content_type)


# ---------------------------------------------------------------------------
# 占位端点（内核暂无对应后端能力）
# ---------------------------------------------------------------------------

_PLACEHOLDER_REASONS = {
    "images": "image-generation",
    "moderations": "moderation",
    "responses": "responses",
    "files": "file-storage",
    "batches": "batch",
    "fine_tuning/jobs": "fine-tuning",
    "assistants": "assistant",
    "vector_stores": "vector-store",
}

_PLACEHOLDER_ROUTES: tuple[tuple[str, list[str]], ...] = (
    ("images/generations", ["POST"]),
    ("images/edits", ["POST"]),
    ("images/variations", ["POST"]),
    ("moderations", ["POST"]),
    ("responses", ["POST"]),
    ("files", ["GET", "POST"]),
    ("batches", ["GET", "POST"]),
    ("fine_tuning/jobs", ["GET", "POST"]),
    ("assistants", ["GET", "POST"]),
    ("vector_stores", ["GET", "POST"]),
)


def _make_placeholder_handler(path: str):
    reason = _PLACEHOLDER_REASONS.get(path, "backend")

    async def handler(request: Request, _key: Any = Depends(require_openai_key)):
        del request
        raise UnsupportedEndpointError(f"/v1/{path}", reason)

    return handler


for _path, _methods in _PLACEHOLDER_ROUTES:
    v1_router.add_api_route(
        f"/{_path}",
        _make_placeholder_handler(_path),
        methods=_methods,
        name=f"unsupported_{_path.replace('/', '_')}",
    )


@v1_router.get("/health")
async def health(request: Request):
    """无需鉴权的存活探测。"""
    service = get_service(request)
    return {
        "status": "ok",
        "enabled": service.is_enabled(),
        "object": "astrbot.openai_compat",
    }


# ---------------------------------------------------------------------------
# 管理面（/api/v1/openai-compat）
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    remark: str = ""
    username: str | None = None


class UpdateKeyRequest(BaseModel):
    enabled: bool | None = None


class UpdateConfigRequest(BaseModel):
    model_config = {"extra": "allow"}

    enabled: bool | None = None
    default_username: str | None = None
    default_model: str | None = None
    request_timeout: int | None = None
    model_aliases: dict[str, str] | None = None


@admin_router.get("/config")
async def get_config(request: Request):
    return {"data": get_service(request).public_config()}


@admin_router.patch("/config")
async def patch_config(payload: UpdateConfigRequest, request: Request):
    return {
        "data": get_service(request).update_config(payload.model_dump(exclude_none=True))
    }


@admin_router.get("/keys")
async def list_keys(request: Request):
    return {"data": get_service(request).list_keys()}


@admin_router.post("/keys")
async def create_key(payload: CreateKeyRequest, request: Request):
    return {"data": get_service(request).create_key(payload.remark, payload.username)}


@admin_router.patch("/keys/{key_id}")
async def update_key(key_id: str, payload: UpdateKeyRequest, request: Request):
    service = get_service(request)
    if payload.enabled is not None and not service.set_key_enabled(
        key_id, payload.enabled
    ):
        return JSONResponse({"message": "key not found"}, status_code=404)
    return {"data": {"id": key_id, "enabled": payload.enabled}}


@admin_router.delete("/keys/{key_id}")
async def delete_key(key_id: str, request: Request):
    if not get_service(request).delete_key(key_id):
        return JSONResponse({"message": "key not found"}, status_code=404)
    return {"data": {"id": key_id, "deleted": True}}


@admin_router.get("/status")
async def admin_status(request: Request):
    service = get_service(request)
    return {
        "data": {
            "enabled": service.is_enabled(),
            "models": service.list_models(),
            "keys": len(service.list_keys()),
        }
    }
