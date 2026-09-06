"""Async orchestration with validated, finalized output and atomic memory."""
from __future__ import annotations
import asyncio
import json
import uuid
from contextlib import suppress
from functools import partial
import anyio
from fastapi import HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from app import memory_store
from app.agent_adapter import (apply_stop_sequences, build_agent_transcript, _user_block,
    build_text_json_response, build_tool_call_json_response, content_to_text,
    parse_tool_calls, sse, stream_text_response, stream_tool_call_response)
from app.conversation import build_lite_transcript, build_standard_transcript
from app.model_registry import is_supported_model, normalize_model_name
from app.notion_client import NotionUpstreamError
from app.proxy_contracts import (ContractError, field, tool_definitions, validate_calls,
                                 validate_request, validate_response_format)


def _next(iterator):
    try:
        return False, next(iterator)
    except StopIteration:
        return True, None


def _owner(client):
    return f'{client.user_id}:{client.space_id}'


async def _close(iterator):
    if iterator is not None and hasattr(iterator, 'close'):
        with anyio.CancelScope(shield=True):
            with suppress(Exception):
                await run_in_threadpool(iterator.close)


def _error(exc, status=502):
    code = getattr(exc, 'status_code', None)
    if code in (429, 503):
        status = code
    message = str(exc) if isinstance(exc, ContractError) else 'The upstream request did not complete successfully.'
    headers = {'Retry-After': str(max(1, int(getattr(exc, 'retry_after', None) or 60)))} if code == 429 else {}
    return JSONResponse({'error': {'message': message, 'type': 'upstream_error', 'code': code or 'INVALID_UPSTREAM_RESPONSE'}}, status_code=status, headers=headers)


def _chunk(response_id, model, delta=None, finish=None, **extra):
    import time
    return sse({'id': response_id, 'object': 'chat.completion.chunk', 'created': int(time.time()),
                'model': model, 'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}], **extra})


async def _lease(request, conversation_id):
    if not conversation_id:
        return lambda: None
    locks = getattr(request.app.state, 'conversation_locks', None)
    if locks is None:
        locks = {}
        request.app.state.conversation_locks = locks
    entry = locks.setdefault(conversation_id, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        await entry[0].acquire()
    except BaseException:
        entry[1] -= 1
        if entry[1] == 0:
            locks.pop(conversation_id, None)
        raise
    released = False
    def release():
        nonlocal released
        if released:
            return
        released = True
        entry[0].release()
        entry[1] -= 1
        if entry[1] == 0:
            locks.pop(conversation_id, None)
    return release


def _prompt_and_history(req):
    instructions = [content_to_text(message.content) for message in req.messages if message.role in ('system', 'developer')]
    dialogue = [message for message in req.messages if message.role not in ('system', 'developer')]
    if not dialogue or dialogue[-1].role != 'user' or not content_to_text(dialogue[-1].content).strip():
        raise ContractError('The last dialogue message must be a nonempty user message.')
    raw = content_to_text(dialogue[-1].content)
    prompt = ('[System/developer instructions]\n' + '\n'.join(instructions) + '\n\n' if instructions else '') + raw
    history = [(message.role, content_to_text(message.content), field(message, 'thinking', '') or '') for message in dialogue[:-1]]
    return prompt, raw, history


def _response_instructions(transcript, req, account, mode):
    instructions = []
    if req.parallel_tool_calls is False and req.tools:
        instructions.append('Return at most one external function call in this response. Do not combine parallel calls.')
    if mode != 'agent' and req.response_format and req.response_format.get('type') != 'text':
        instructions.append('Return JSON only conforming to this response format: ' + json.dumps(req.response_format, ensure_ascii=False))
    if instructions:
        transcript.insert(1 if mode == 'lite' else 2, _user_block('\n'.join(instructions), account))


async def _open(request, req, mode, manager, conversation_id):
    pool = request.app.state.account_pool
    binding = await run_in_threadpool(memory_store.thread_binding, manager, conversation_id) if manager else {}
    last_error = None
    for _ in range(min(max(len(pool.clients), 1), 3)):
        iterator, client = None, None
        try:
            client = await run_in_threadpool(partial(pool.get_client, wait_if_cooling=False, preferred_owner=binding.get('thread_owner')))
            owner = _owner(client)
            account = {'user_id': client.user_id, 'space_id': client.space_id}
            if mode == 'agent':
                transcript = build_agent_transcript(req.messages, req.model, account, req.tools, req.tool_choice, req.response_format)
                degraded = False
            elif mode == 'lite':
                prompt, _, _ = _prompt_and_history(req)
                transcript, degraded = build_lite_transcript(prompt, req.model), False
            elif mode == 'standard':
                transcript = build_standard_transcript([message.model_dump() for message in req.messages], req.model, account)
                degraded = False
            else:
                prompt, _, _ = _prompt_and_history(req)
                transcript, degraded = await run_in_threadpool(memory_store.build_payload, manager, conversation_id, prompt, client, req.model)
            _response_instructions(transcript, req, account, mode)
            reusable = bool(binding.get('thread_id') and binding.get('thread_owner') == owner and binding.get('thread_model') == req.model)
            thread_id = binding['thread_id'] if reusable else str(uuid.uuid4())
            iterator = client.stream_response(transcript, thread_id=thread_id, new_thread=not reusable, agent_mode=(mode == 'agent'))
            done, first = await run_in_threadpool(_next, iterator)
            if done:
                raise NotionUpstreamError('Empty upstream stream.', status_code=502)
            return client, iterator, first, thread_id, degraded
        except NotionUpstreamError as exc:
            await _close(iterator)
            last_error = exc
            if client is not None:
                pool.mark_failed(client, float('inf') if exc.status_code in (401, 403) else getattr(exc, 'retry_after', None) or 5)
            if exc.status_code == 429 or (not exc.retriable and exc.status_code not in (401, 403)):
                raise
            binding = {}
        except BaseException:
            await _close(iterator)
            raise
    raise last_error or NotionUpstreamError('No usable upstream account.', status_code=503)


async def _items(first, iterator):
    value = first
    while True:
        if isinstance(value, str):
            yield {'type': 'content', 'text': value}
        elif isinstance(value, dict):
            if value.get('type') in ('error', 'failed') or value.get('error'):
                raise NotionUpstreamError('Upstream reported failure.', status_code=502)
            yield value
        done, value = await run_in_threadpool(_next, iterator)
        if done:
            break


async def complete(request, req, response, background_tasks=None, mode='heavy'):
    background_tasks = background_tasks if background_tasks is not None else BackgroundTasks()
    req.model = normalize_model_name(req.model)
    try:
        validate_request(req)
        if not is_supported_model(req.model):
            raise ContractError('Unsupported model.')
        if mode != 'agent':
            _prompt_and_history(req)
    except ContractError as exc:
        raise HTTPException(400, str(exc)) from exc
    manager = getattr(request.app.state, 'conversation_manager', None) if mode == 'heavy' else None
    conversation_id = None
    if mode == 'heavy':
        if manager is None:
            raise HTTPException(503, 'Conversation storage is unavailable.')
        conversation_id = req.conversation_id
        if conversation_id:
            if not await run_in_threadpool(manager.conversation_exists, conversation_id):
                raise HTTPException(404, 'Conversation not found.')
        else:
            conversation_id = await run_in_threadpool(manager.new_conversation)
    release = await _lease(request, conversation_id)
    iterator, streaming, succeeded = None, False, False
    response_id = 'chatcmpl-' + uuid.uuid4().hex

    async def cleanup():
        try:
            await _close(iterator)
            if manager and not succeeded:
                with anyio.CancelScope(shield=True):
                    await run_in_threadpool(manager.clear_conversation_thread, conversation_id)
        finally:
            release()

    try:
        if manager:
            _, _, history = _prompt_and_history(req)
            try:
                await run_in_threadpool(memory_store.import_history, manager, conversation_id, history)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        client, iterator, first, thread_id, degraded = await _open(request, req, mode, manager, conversation_id)
        headers = {'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
        if conversation_id:
            headers.update({'X-Conversation-Id': conversation_id, 'X-Memory-Status': 'degraded' if degraded else 'ok'})
        web = request.headers.get('X-Client-Type', '').lower() == 'web'
        provisional_body = web and not req.stop and not req.response_format

        async def finish(text, thinking):
            original = text
            text = apply_stop_sequences(text, req.stop)
            validate_response_format(text, req.response_format)
            # A valid stop sequence at offset zero may produce an empty answer.
            if not original.strip() and not thinking.strip():
                raise NotionUpstreamError('Empty upstream answer.', status_code=502)
            if manager:
                _, raw, _ = _prompt_and_history(req)
                await run_in_threadpool(memory_store.persist_round, manager, conversation_id, raw, text, thinking)
                await run_in_threadpool(memory_store.bind_thread, manager, conversation_id, thread_id, req.model, _owner(client))
                background_tasks.add_task(memory_store.compress_overflow, manager, conversation_id)
            return text

        async def stream():
            nonlocal succeeded
            text, thinking, emitted, emitted_thinking = '', '', '', ''
            try:
                yield _chunk(response_id, req.model, {'role': 'assistant'})
                async for item in _items(first, iterator):
                    kind, value = item.get('type'), str(item.get('text') or '')
                    if kind == 'content':
                        text += value
                        if provisional_body:
                            emitted += value
                            yield _chunk(response_id, req.model, {'content': value})
                    elif kind in ('content_replace', 'final_content'):
                        text = value
                        if kind == 'content_replace' and provisional_body:
                            emitted = value
                            yield _chunk(response_id, req.model, type='content_replace', content=value)
                    elif kind == 'thinking' and mode != 'lite':
                        thinking += value
                        if web:
                            emitted_thinking += value
                            yield _chunk(response_id, req.model, {'reasoning_content': value})
                    elif kind in ('thinking_replace', 'final_thinking') and mode != 'lite':
                        thinking = value
                        if kind == 'thinking_replace' and web:
                            emitted_thinking = value
                            yield _chunk(response_id, req.model, type='thinking_replace', thinking=value)
                    elif kind == 'search' and mode != 'lite' and web:
                        yield _chunk(response_id, req.model, type='search_metadata', searches=item.get('data', {}))
                text = await finish(text, thinking)
                if thinking.startswith(emitted_thinking):
                    if thinking[len(emitted_thinking):]:
                        yield _chunk(response_id, req.model, {'reasoning_content': thinking[len(emitted_thinking):]})
                elif web:
                    yield _chunk(response_id, req.model, type='thinking_replace', thinking=thinking)
                if text.startswith(emitted):
                    if text[len(emitted):]:
                        yield _chunk(response_id, req.model, {'content': text[len(emitted):]})
                elif web:
                    yield _chunk(response_id, req.model, type='content_replace', content=text)
                else:
                    raise ContractError('Final text cannot replace emitted content.')
                succeeded = True
                yield _chunk(response_id, req.model, finish='stop')
                yield 'data: [DONE]\n\n'
            except Exception:
                yield sse({'error': {'message': 'The response was interrupted or failed validation.', 'type': 'upstream_error'}})
                yield 'data: [DONE]\n\n'
            finally:
                await cleanup()

        if req.stream and mode != 'agent':
            streaming = True
            return StreamingResponse(stream(), media_type='text/event-stream', headers=headers, background=background_tasks)
        text, thinking = '', ''
        async for item in _items(first, iterator):
            kind, value = item.get('type'), str(item.get('text') or '')
            if kind == 'content':
                text += value
            elif kind in ('content_replace', 'final_content'):
                text = value
            elif kind == 'thinking' and mode != 'lite':
                thinking += value
            elif kind in ('thinking_replace', 'final_thinking') and mode != 'lite':
                thinking = value
        if mode == 'agent':
            definitions = tool_definitions(req.tools)
            calls = parse_tool_calls(text, set(definitions), parallel_tool_calls=req.parallel_tool_calls)
            validate_calls(calls, req.tools, req.tool_choice, req.parallel_tool_calls)
            if calls:
                result = build_tool_call_json_response(response_id, req.model, calls)
                output = stream_tool_call_response(response_id, req.model, calls)
            else:
                text = await finish(text, thinking)
                result = build_text_json_response(response_id, req.model, text)
                output = stream_text_response(response_id, req.model, text)
            if req.stream:
                succeeded = True
                return StreamingResponse(output, media_type='text/event-stream', headers=headers)
        else:
            text = await finish(text, thinking)
            result = build_text_json_response(response_id, req.model, text)
            if thinking and mode != 'lite':
                result['choices'][0]['message']['reasoning_content'] = thinking
        succeeded = True
        return JSONResponse(result, headers=headers, background=background_tasks)
    except (ContractError, NotionUpstreamError) as exc:
        return _error(exc)
    except RuntimeError:
        return _error(NotionUpstreamError('Account or upstream processing unavailable.', status_code=503), 503)
    finally:
        if not streaming:
            await cleanup()
