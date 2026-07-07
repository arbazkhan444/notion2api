import json
import re
import time
import uuid
from typing import Any


def _get_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _get_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif "text" in item:
                    parts.append(str(item.get("text") or ""))
        return "\n".join(parts)
    return str(content)


def _last_user_text(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if _get_role(msg) == "user":
            return _content_to_text(_get_content(msg))
    return ""


def _has_tool_result(messages: list[Any]) -> bool:
    return any(_get_role(msg) in {"tool", "function"} for msg in messages)


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        return tool
    if hasattr(tool, "dict"):
        return tool.dict()
    return {}


def _tool_name(tool: Any) -> str:
    raw = _tool_to_dict(tool)
    if raw.get("type") == "function":
        fn = raw.get("function") or {}
        return str(fn.get("name") or "")
    return str(raw.get("name") or "")


def _find_tool(tools: list[Any], names: list[str]) -> str | None:
    available = {_tool_name(tool): tool for tool in tools}

    for name in names:
        if name in available:
            return name

    lowered = {name.lower(): name for name in available}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]

    return None


def _build_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def has_tool_result(messages: list[Any]) -> bool:
    return _has_tool_result(messages)


def find_tool_name(tools: list[Any], candidates: list[str]) -> str | None:
    return _find_tool(tools, candidates)


def build_single_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _build_tool_call(name, arguments)


def _extract_requested_path(text: str) -> str | None:
    package_match = re.search(r"(?:^|[\s`'\"])([\w./\\-]*package\.json)(?:$|[\s`'\"])", text, re.I)
    if package_match:
        return package_match.group(1).replace("\\", "/").lstrip("./") or "package.json"
    return None


def build_direct_tool_call_response(req_body: Any, model: str):
    """
    Return a direct OpenAI tool call before Notion is contacted.

    This handles simple first-step Cline tasks where tool selection is obvious.
    Once Cline sends tool results back, the request falls through to Notion.
    """
    tools = list(getattr(req_body, "tools", None) or [])
    messages = list(getattr(req_body, "messages", None) or [])

    if not tools or _has_tool_result(messages):
        return None

    user_text = _last_user_text(messages)
    lowered = user_text.lower()
    response_id = f"chatcmpl-{uuid.uuid4().hex}"

    requested_path = _extract_requested_path(user_text)
    if requested_path:
        tool_name = _find_tool(tools, ["read_file"])
        if tool_name:
            return response_id, _build_tool_call(tool_name, {"path": requested_path})

    if "zustand" in lowered:
        tool_name = _find_tool(tools, ["search_files", "grep_search", "search"])
        if tool_name:
            return response_id, _build_tool_call(
                tool_name,
                {"path": ".", "regex": "zustand", "file_pattern": "*.ts*"},
            )

        tool_name = _find_tool(tools, ["read_file"])
        if tool_name:
            return response_id, _build_tool_call(tool_name, {"path": "package.json"})

    if any(
        word in lowered
        for word in [
            "framework",
            "app",
            "codebase",
            "project",
            "used in this app",
            "how does",
        ]
    ):
        tool_name = _find_tool(tools, ["list_files"])
        if tool_name:
            return response_id, _build_tool_call(
                tool_name, {"path": ".", "recursive": False}
            )

    return None


def direct_tool_response_payload(
    response_id: str, model: str, tool_call: dict[str, Any]
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def direct_tool_stream(response_id: str, model: str, tool_call: dict[str, Any]):
    created = int(time.time())

    yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': tool_call['id'], 'type': 'function', 'function': {'name': tool_call['function']['name'], 'arguments': tool_call['function']['arguments']}}]}, 'finish_reason': None}]}, ensure_ascii=False)}\n\n"
    yield f"data: {json.dumps({'id': response_id, 'object': 'chat.completion.chunk', 'created': created, 'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}]}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"
