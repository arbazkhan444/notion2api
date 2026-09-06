"""Ordered, schema-validated adapter helpers for OpenAI function clients."""
import json
import time
import uuid
from datetime import datetime
from app.model_registry import get_notion_model, get_thread_type

TOOL_CALL_TAG = 'openai_tool_call'
TOOL_CALLS_TAG = 'openai_tool_calls'


def _get_field(value, field, default=None):
    return value.get(field, default) if isinstance(value, dict) else getattr(value, field, default)


def is_agent_request(req):
    return bool(_get_field(req, 'tools')) or any(
        _get_field(message, 'role') in ('tool', 'function') or _get_field(message, 'tool_calls') or _get_field(message, 'function_call')
        for message in _get_field(req, 'messages', []) or [])


def content_to_text(content):
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(str(item.get('text', '')) for item in content if isinstance(item, dict) and item.get('type') == 'text')
    return str(content)


def tool_result_to_text(message):
    return '[Tool result data]\n' + json.dumps({key: _get_field(message, key) for key in ('tool_call_id', 'name', 'content')}, ensure_ascii=False)


def assistant_tool_calls_to_text(message):
    return content_to_text(_get_field(message, 'content')) + '\n' + json.dumps(
        {'tool_calls': _get_field(message, 'tool_calls'), 'function_call': _get_field(message, 'function_call')}, ensure_ascii=False)


def get_safe_system_parts(messages, *, has_tools):
    return [content_to_text(_get_field(m, 'content')) for m in messages if _get_field(m, 'role') in ('system', 'developer')]


def _config_block(model_name):
    return {'id': str(uuid.uuid4()), 'type': 'config', 'value': {
        'type': get_thread_type(model_name), 'model': get_notion_model(model_name),
        'modelFromUser': True, 'useWebSearch': False, 'useReadOnlyMode': True}}


def _context_block(account):
    return {'id': str(uuid.uuid4()), 'type': 'context', 'value': {
        'timezone': 'UTC', 'currentDatetime': datetime.now().astimezone().isoformat(),
        'userId': account.get('user_id', ''), 'spaceId': account.get('space_id', '')}}


def _user_block(content, account=None):
    block = {'id': str(uuid.uuid4()), 'type': 'user', 'value': [[content]]}
    if account:
        block.update(userId=account.get('user_id', ''), createdAt=datetime.now().astimezone().isoformat())
    return block


def _assistant_block(content):
    return {'id': str(uuid.uuid4()), 'type': 'agent-inference', 'value': [{'type': 'text', 'content': content}]}


def build_agent_transcript(messages, model_name, account, tools=None, tool_choice=None,
                           response_format=None, max_tokens=None, max_completion_tokens=None):
    from app.proxy_contracts import build_transcript
    return build_transcript(messages, model_name, account, tools, tool_choice, response_format, max_tokens, max_completion_tokens)


def build_tool_adapter_prompt(tools, tool_choice=None, response_format=None, max_tokens=None, max_completion_tokens=None):
    from app.proxy_contracts import tool_definitions, validate_tool_choice
    definitions = tool_definitions(tools)
    validate_tool_choice(tool_choice, definitions)
    return json.dumps({'tools': tools or [], 'tool_choice': tool_choice, 'response_format': response_format}, ensure_ascii=False)


def parse_tool_calls(text, allowed_tool_names, *, parallel_tool_calls=None):
    from app.proxy_contracts import parse_calls
    return parse_calls(text, allowed_tool_names, parallel_tool_calls=parallel_tool_calls)


def apply_stop_sequences(text, stop):
    if not stop:
        return text
    stops = [stop] if isinstance(stop, str) else stop
    cuts = [text.find(item) for item in stops if item and text.find(item) >= 0]
    return text[:min(cuts)] if cuts else text


def sse(payload):
    return 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'


def build_text_json_response(response_id, model, text):
    return {'id': response_id, 'object': 'chat.completion', 'created': int(time.time()), 'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}


def build_tool_call_json_response(response_id, model, tool_calls):
    result = build_text_json_response(response_id, model, None)
    result['choices'][0]['message']['tool_calls'] = tool_calls
    result['choices'][0]['finish_reason'] = 'tool_calls'
    return result


def _chunk(response_id, model, delta, finish=None):
    return sse({'id': response_id, 'object': 'chat.completion.chunk', 'created': int(time.time()), 'model': model,
                'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})


def stream_text_response(response_id, model, text):
    yield _chunk(response_id, model, {'role': 'assistant'})
    if text:
        yield _chunk(response_id, model, {'content': text})
    yield _chunk(response_id, model, {}, 'stop')
    yield 'data: [DONE]\n\n'


def stream_tool_call_response(response_id, model, tool_calls):
    yield _chunk(response_id, model, {'role': 'assistant'})
    for index, call in enumerate(tool_calls):
        yield _chunk(response_id, model, {'tool_calls': [{'index': index, **call}]})
    yield _chunk(response_id, model, {}, 'tool_calls')
    yield 'data: [DONE]\n\n'
