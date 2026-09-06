"""Compatibility exports. No substring-based automatic function selection."""
import json
import re
import uuid
from app.agent_adapter import build_tool_call_json_response, stream_tool_call_response


def build_direct_tool_call_response(req_body, model_name):
    return None


def _extract_requested_path(text):
    match = re.search(r"(?:^|[\s`'\"])((?:[A-Za-z]:)?[\w./\\-]*package\.json)(?:$|[\s`'\"])", text, re.I)
    if not match:
        return 'package.json'
    return match.group(1).replace('\\', '/').removeprefix('./')


def build_single_tool_call(name, arguments):
    if not isinstance(arguments, dict):
        raise ValueError('Function arguments must be an object.')
    return {'id': 'call_' + uuid.uuid4().hex[:24], 'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}


def find_tool_name(tools, names):
    available = {tool.get('function', {}).get('name') for tool in tools if isinstance(tool, dict)}
    return next((name for name in names if name in available), None)


def has_tool_result(messages):
    return any((message.get('role') if isinstance(message, dict) else getattr(message, 'role', None)) in ('tool', 'function') for message in messages)


def direct_tool_response_payload(response_id, model, tool_call):
    return build_tool_call_json_response(response_id, model, [tool_call])


def direct_tool_stream(response_id, model, tool_call):
    yield from stream_tool_call_response(response_id, model, [tool_call])
