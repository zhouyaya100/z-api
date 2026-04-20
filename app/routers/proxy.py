"""Z API - OpenAI 兼容代理路由

职责：HTTP 入口，调 core 层的 routing/quota/token_count
"""
import httpx
import json
import time
import uuid
import logging
from fastapi import Request, HTTPException, Header
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy import select

from ..database import AsyncSessionLocal
from ..models import Channel, Token, Log, User, Group
from ..config import settings
from ..core.routing import channel_pool, routing_engine, RoutingStrategy
from ..core.quota import quota_checker, quota_deductor
from ..core.token_count import count_prompt_tokens, count_tokens
from ..core.rate_limit import rate_limiter
from ..core.log_writer import log_writer
from ..core.error_log import error_logger

logger = logging.getLogger("z-api")

# ---- 全局 httpx 连接池 ----
_http_client: httpx.AsyncClient | None = None


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=settings.PROXY_TIMEOUT,
            limits=httpx.Limits(
                max_connections=settings.PROXY_MAX_CONNECTIONS,
                max_keepalive_connections=settings.PROXY_MAX_KEEPALIVE,
                keepalive_expiry=settings.PROXY_KEEPALIVE_EXPIRY,
            ),
        )
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None


# ---- Token 验证 ----
async def validate_token(api_key: str, db, model: str = None) -> tuple:
    """验证 API Key + 配额检查，返回 (token, user)"""
    if not api_key or not api_key.startswith("sk-"):
        raise HTTPException(401, detail={"error": {"message": "无效的 API Key 格式", "type": "invalid_request_error", "code": "invalid_api_key"}})

    result = await db.execute(select(Token).where(Token.key == api_key, Token.enabled == True))
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(401, detail={"error": {"message": "无效的 API Key", "type": "invalid_request_error", "code": "invalid_api_key"}})

    # 模型权限检查
    if token.models:
        allowed = [m.strip() for m in token.models.split(",")]
        if model and model not in allowed:
            raise HTTPException(403, detail={"error": {"message": f"模型 '{model}' 不在令牌授权范围内", "type": "invalid_request_error", "code": "model_not_allowed"}})

    user = await db.get(User, token.user_id) if token.user_id else None

    # 用户模型权限
    if not token.models and user and user.allowed_models:
        allowed = [m.strip() for m in user.allowed_models.split(",")]
        if model and model not in allowed:
            raise HTTPException(403, detail={"error": {"message": f"模型 '{model}' 不在您的授权范围内", "type": "invalid_request_error", "code": "model_not_allowed"}})

    # 配额检查
    token_result = await quota_checker.check_token(token, model)
    quota_checker.raise_if_failed(token_result)

    if user:
        user_result = await quota_checker.check_user(user)
        quota_checker.raise_if_failed(user_result)

    return token, user


# ---- 渠道失败处理 ----
async def handle_channel_failure(db, channel_info):
    """渠道失败：更新 DB + 渠道池索引"""
    ch = await db.get(Channel, channel_info.id)
    if ch:
        ch.fail_count = (ch.fail_count or 0) + 1
        if ch.auto_ban and ch.fail_count >= 5:
            ch.enabled = False
        await db.commit()
        # 同步更新渠道池
        channel_pool.update_fail_count(channel_info.id, ch.fail_count, ch.enabled)


async def handle_channel_success(db, channel_info):
    """渠道成功：重置失败计数"""
    ch = await db.get(Channel, channel_info.id)
    if ch and ch.fail_count and ch.fail_count > 0:
        ch.fail_count = 0
        await db.commit()
        channel_pool.update_fail_count(channel_info.id, 0, True)


# ---- 核心转发 ----
async def proxy_request(request: Request):
    request_id = uuid.uuid4().hex[:8]
    auth = request.headers.get("Authorization", "")
    api_key = auth.replace("Bearer ", "").strip() if auth.startswith("Bearer ") else ""
    body_bytes = await request.body()
    try:
        body_json = json.loads(body_bytes)
    except:
        body_json = {}
    original_model = body_json.get("model", "")
    is_stream = body_json.get("stream", False)
    client_ip = request.client.host if request.client else ""

    # Rate limiting
    if rate_limiter:
        reject_reason = rate_limiter.check(api_key, client_ip)
        if reject_reason:
            raise HTTPException(429, detail={"error": {"message": reject_reason, "type": "rate_limit_exceeded", "code": "rate_limit_exceeded"}})

    # Inject stream_options
    body_modified = False
    if is_stream and isinstance(body_json, dict):
        if "stream_options" not in body_json:
            body_json["stream_options"] = {"include_usage": True}
            body_modified = True
        elif isinstance(body_json.get("stream_options"), dict):
            if not body_json["stream_options"].get("include_usage"):
                body_json["stream_options"]["include_usage"] = True
                body_modified = True

    async with AsyncSessionLocal() as db:
        # 尝试反向查找映射：如果用户用映射后名称调用，找到映射前名称做权限检查
        check_model = original_model
        # 使用渠道池的反向映射索引
        reverse_mapped = channel_pool.reverse_map(original_model)
        if reverse_mapped and reverse_mapped != original_model:
            check_model = reverse_mapped

        token, user = await validate_token(api_key, db, check_model)

        # 获取用户分组名
        group_name = None
        if user and user.group_id:
            grp = await db.get(Group, user.group_id)
            if grp:
                group_name = grp.name

        # Retry with channel failover
        max_retries = settings.PROXY_RETRY_COUNT
        exclude_channel_ids = set()

        for attempt in range(max_retries + 1):
            # 从渠道池选择（O(1) 倒排索引）
            channel_info = channel_pool.select(
                original_model, group=group_name,
                exclude_ids=exclude_channel_ids if attempt > 0 else None
            )
            if not channel_info:
                if attempt > 0:
                    break
                raise HTTPException(404, detail={
                    "error": {"message": f"模型 '{original_model}' 没有可用渠道",
                              "type": "invalid_request_error", "code": "model_not_found"}
                })
            # 检查分组（如果没有分组，只允许不限制分组的渠道）
            # 不再直接 403，而是用 group=None 继续查找渠道池

            # 模型映射
            mapped_model = routing_engine.resolve_model(channel_info, original_model)
            if mapped_model != original_model:
                body_json["model"] = mapped_model
                body_bytes = json.dumps(body_json).encode()
            elif body_modified:
                body_bytes = json.dumps(body_json).encode()

            # 构建上游请求
            upstream_url = routing_engine.build_upstream_url(channel_info, request.url.path)
            headers = routing_engine.build_headers(channel_info, dict(request.headers))
            start = time.time()

            try:
                if is_stream:
                    return await _stream_proxy(upstream_url, headers, body_bytes, body_json, db, token, user, channel_info, original_model, mapped_model, is_stream, client_ip, start)
                else:
                    return await _non_stream_proxy(upstream_url, headers, body_bytes, body_json, db, token, user, channel_info, original_model, mapped_model, is_stream, client_ip, start)
            except (httpx.TimeoutException, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                latency_ms = int((time.time() - start) * 1000)
                await handle_channel_failure(db, channel_info)
                await _add_log(token, user, channel_info, original_model, is_stream, 0, 0, latency_ms, False, "Upstream timeout", client_ip)
                error_logger.error(f"[{request_id}] Upstream timeout: channel={channel_info.name} model={original_model} attempt={attempt+1}/{max_retries}", exc_info=True)
                exclude_channel_ids.add(channel_info.id)
                if attempt < max_retries:
                    logger.info(f"Retry {attempt+1}/{max_retries}: channel '{channel_info.name}' timeout, switching...")
                    continue
                raise HTTPException(504, detail={"error": {"message": "上游服务超时", "type": "upstream_timeout", "code": "timeout"}})
            except HTTPException:
                raise
            except Exception as e:
                latency_ms = int((time.time() - start) * 1000)
                await handle_channel_failure(db, channel_info)
                await _add_log(token, user, channel_info, original_model, is_stream, 0, 0, latency_ms, False, str(e)[:200], client_ip)
                error_logger.error(f"[{request_id}] Proxy error: channel={channel_info.name} model={original_model} attempt={attempt+1}/{max_retries}", exc_info=True)
                exclude_channel_ids.add(channel_info.id)
                if attempt < max_retries:
                    logger.info(f"Retry {attempt+1}/{max_retries}: channel '{channel_info.name}' error, switching...")
                    continue
                raise HTTPException(502, detail={"error": {"message": "上游服务错误", "type": "upstream_error", "code": "bad_gateway"}})

        raise HTTPException(502, detail={"error": {"message": "上游服务不可用", "type": "upstream_error", "code": "bad_gateway"}})


async def _non_stream_proxy(upstream_url, headers, body, body_json, db, token, user, channel_info,
                             original_model, mapped_model, is_stream, client_ip, start):
    client = await get_http_client()
    resp = await client.post(upstream_url, content=body, headers=headers)
    latency_ms = int((time.time() - start) * 1000)
    if resp.status_code != 200:
        error_msg = resp.text[:500]
        await handle_channel_failure(db, channel_info)
        try:
            error_json = resp.json()
        except:
            error_json = {"error": {"message": "上游服务错误", "type": "upstream_error"}}
        await _add_log(token, user, channel_info, original_model, is_stream, 0, 0, latency_ms, False, error_msg, client_ip)
        if "error" in error_json and isinstance(error_json["error"], dict):
            msg = error_json["error"].get("message", "")
            if any(x in msg.lower() for x in ["traceback", "exception", "file ", "line ", "python"]):
                error_json["error"]["message"] = "上游服务内部错误"
        return JSONResponse(status_code=resp.status_code, content=error_json)
    await handle_channel_success(db, channel_info)
    result_json = resp.json()
    usage = result_json.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    if not prompt_tokens and body_json:
        prompt_tokens = count_prompt_tokens(body_json)
    total_tokens = prompt_tokens + completion_tokens
    # 批量配额扣减（合并队列）
    await quota_deductor.deduct(token.id, token.user_id, total_tokens)
    await _add_log(token, user, channel_info, original_model, is_stream, prompt_tokens, completion_tokens, latency_ms, True, "", client_ip)
    return JSONResponse(content=result_json)


async def _stream_proxy(upstream_url, headers, body, body_json, db, token, user, channel_info,
                         original_model, mapped_model, is_stream, client_ip, start):
    token_id = token.id
    token_name = token.name
    channel_id = channel_info.id
    channel_name = channel_info.name
    user_id = user.id if user else 0
    user_id_val = token.user_id
    auto_ban = channel_info.auto_ban
    counted_prompt = count_prompt_tokens(body_json) if body_json else 0

    async def generate():
        completion_tokens = 0
        prompt_tokens = 0
        has_usage = False
        success = False
        error_msg = ""
        try:
            client = await get_http_client()
            async with client.stream("POST", upstream_url, content=body, headers=headers) as resp:
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_msg = error_body.decode()[:500]
                    yield f"data: {error_msg}\n\n"
                    return
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            success = True
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            if "content" in delta and delta["content"]:
                                completion_tokens += count_tokens(delta["content"])
                            usage = chunk.get("usage", {})
                            if usage:
                                has_usage = True
                                prompt_tokens = usage.get("prompt_tokens", prompt_tokens)
                                if usage.get("completion_tokens"):
                                    completion_tokens = usage["completion_tokens"]
                        except:
                            pass
                    yield f"{line}\n\n"
        except Exception as e:
            error_msg = str(e)[:200]
            error_logger.error(f"[{request_id}] Stream error: channel={channel_name} model={original_model}", exc_info=True)
            yield f"data: {{\"error\": {{\"message\": \"流式传输中断\"}}}}\n\n"
        latency_ms = int((time.time() - start) * 1000)
        final_prompt = prompt_tokens if has_usage else counted_prompt
        final_completion = completion_tokens
        total_used = final_prompt + final_completion

        try:
            async with AsyncSessionLocal() as db2:
                if success:
                    if total_used > 0:
                        await quota_deductor.deduct(token_id, user_id_val, total_used)
                    ch = await db2.get(Channel, channel_id)
                    if ch and ch.fail_count and ch.fail_count > 0:
                        ch.fail_count = 0
                        await db2.commit()
                        channel_pool.update_fail_count(channel_id, 0, True)
                else:
                    ch = await db2.get(Channel, channel_id)
                    if ch:
                        ch.fail_count = (ch.fail_count or 0) + 1
                        if auto_ban and ch.fail_count >= 5:
                            ch.enabled = False
                        await db2.commit()
                        channel_pool.update_fail_count(channel_id, ch.fail_count, ch.enabled)
                await log_writer.add(Log(
                    user_id=user_id, token_id=token_id, token_name=token_name,
                    channel_id=channel_id, channel_name=channel_name,
                    model=original_model, is_stream=is_stream,
                    prompt_tokens=final_prompt, completion_tokens=final_completion,
                    latency_ms=latency_ms, success=success, error_msg=error_msg[:500],
                    client_ip=client_ip,
                ))
        except Exception as e:
            error_logger.error(f"[{request_id}] Post-stream DB error: {e}", exc_info=True)

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _add_log(token, user, channel_info, model, is_stream,
                   prompt_tokens, completion_tokens, latency_ms, success, error_msg, client_ip):
    try:
        await log_writer.add(Log(
            user_id=user.id if user else 0,
            token_id=token.id, token_name=token.name,
            channel_id=channel_info.id, channel_name=channel_info.name,
            model=model, is_stream=is_stream,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            latency_ms=latency_ms, success=success, error_msg=error_msg[:500],
            client_ip=client_ip,
        ))
    except Exception:
        try:
            async with AsyncSessionLocal() as db2:
                db2.add(Log(
                    user_id=user.id if user else 0,
                    token_id=token.id, token_name=token.name,
                    channel_id=channel_info.id, channel_name=channel_info.name,
                    model=model, is_stream=is_stream,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    latency_ms=latency_ms, success=success, error_msg=error_msg[:500],
                    client_ip=client_ip,
                ))
                await db2.commit()
        except:
            pass
