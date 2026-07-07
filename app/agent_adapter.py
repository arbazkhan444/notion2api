import json
import re
import time
import uuid
from datetime import datetime
from typing import Any, Iterable

from app.model_registry import get_notion_model, get_thread_type

TOOL_CALL_TAG = "openai_tool_call"
TOOL_CALLS_TAG = "openai_tool_calls"
CLIENT_RUNTIME_MARKERS = (
    "cline",
    "kiro",
    "mcp",
    "tool use",
    "tool-use",
    "read_file",
    "write_to_file",
    "replace_in_file",
    "execute_command",
    "browser_action",
    "attempt_completion",
    "ask_followup_question",
)

_SINGLE_RE = re.compile(
    rf"<{TOOL_CALL_TAG}>\s*(.*?)\s*</{TOOL_CALL_TAG}>",
    re.DOTALL,
)
_MULTI_RE = re.compile(
    rf"<{TOOL_CALLS_TAG}>\s*(.*?)\s*</{TOOL_CALLS_TAG}>",
    re.DOTALL,
)


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tool_to_dict(tool: Any) -> dict[str, Any]:
    if hasattr(tool, "dict"):
        return tool.dict()
    if isinstance(tool, dict):
        return tool
    return {}


def _get_field(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def _tool_call_function(call: Any) -> Any:
    return _get_field(call, "function", {}) or {}


def _tool_call_name(call: Any) -> str:
    return str(_get_field(_tool_call_function(call), "name", "") or "")


def _tool_call_arguments(call: Any) -> Any:
    return _get_field(_tool_call_function(call), "arguments", "{}")


def is_agent_request(req: Any) -> bool:
    if getattr(req, "tools", None):
        return True

    for msg in getattr(req, "messages", []) or []:
        if getattr(msg, "role", None) in ("tool", "function"):
            return True
        if getattr(msg, "tool_calls", None) or getattr(msg, "function_call", None):
            return True

    return False


def content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                parts.append(str(item.get("text", "")))
            elif item_type == "image_url":
                parts.append(
                    "[Image input omitted: image_url content is not supported by this proxy yet]"
                )
            elif "text" in item:
                parts.append(str(item.get("text", "")))
        return "\n".join(part for part in parts if part.strip())
    return str(content)


def tool_result_to_text(msg: Any) -> str:
    return (
        "[Tool result]\n"
        f"tool_call_id: {_get_field(msg, 'tool_call_id', '') or ''}\n"
        f"name: {_get_field(msg, 'name', '') or ''}\n"
        f"content:\n{content_to_text(_get_field(msg, 'content', None))}"
    )


def assistant_tool_calls_to_text(msg: Any) -> str:
    tool_calls = getattr(msg, "tool_calls", None)
    function_call = getattr(msg, "function_call", None)
    if not tool_calls and not function_call:
        return content_to_text(getattr(msg, "content", None))

    lines: list[str] = []
    content = content_to_text(getattr(msg, "content", None))
    if content:
        lines.append(content)

    lines.append("[Assistant requested tool calls]")
    for call in tool_calls or []:
        call_id = _get_field(call, "id", "")
        lines.append(
            f"- id: {call_id}\n"
            f"  name: {_tool_call_name(call)}\n"
            f"  arguments: {_tool_call_arguments(call)}"
        )

    if function_call:
        lines.append(f"- legacy_function_call: {_dump_json(function_call)}")

    return "\n".join(lines)


def is_client_runtime_prompt(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(marker in lowered for marker in CLIENT_RUNTIME_MARKERS)


def get_safe_system_parts(messages: list[Any], *, has_tools: bool) -> list[str]:
    safe_parts: list[str] = []

    for msg in messages:
        if getattr(msg, "role", None) != "system":
            continue

        text = content_to_text(getattr(msg, "content", None)).strip()
        if not text:
            continue

        if is_client_runtime_prompt(text):
            continue

        safe_parts.append(text)

    return safe_parts


def build_tool_adapter_prompt(
    tools: Iterable[Any] | None,
    tool_choice: Any = None,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    max_completion_tokens: int | None = None,
) -> str:
    if tool_choice == "none":
        tool_instructions = ""
    else:
        tool_dicts = [_tool_to_dict(tool) for tool in tools or []]
        tool_dicts = [tool for tool in tool_dicts if tool.get("type") == "function"]
        tool_instructions = ""
        if tool_dicts:
            lines = [
                "External functions are available.",
                "When file, terminal, browser, or project access is needed, request a function call using the exact format below.",
                "",
                f"<{TOOL_CALL_TAG}>",
                '{"name":"tool_name","arguments":{"key":"value"}}',
                f"</{TOOL_CALL_TAG}>",
                "",
                "For multiple tool calls:",
                f"<{TOOL_CALLS_TAG}>",
                '[{"name":"tool_name","arguments":{"key":"value"}}]',
                f"</{TOOL_CALLS_TAG}>",
                "",
                "Rules:",
                "- Use only listed function names.",
                "- Arguments must be valid JSON.",
                "- Do not invent fields outside the tool schema unless necessary.",
                "- If no tool is needed, answer normally.",
            ]

            if tool_choice == "required":
                lines.append("- You must call at least one tool.")
            elif isinstance(tool_choice, dict):
                forced_name = (
                    tool_choice.get("function", {}).get("name")
                    if isinstance(tool_choice.get("function"), dict)
                    else None
                )
                if forced_name:
                    lines.append(f"- You must call the tool named {forced_name}.")

            lines.append("")
            lines.append("Available tools:")
            for index, tool in enumerate(tool_dicts, 1):
                function = tool.get("function") or {}
                lines.append(f"{index}. {function.get('name', '')}")
                if function.get("description"):
                    lines.append(f"Description: {function.get('description')}")
                lines.append("JSON schema:")
                lines.append(_dump_json(function.get("parameters") or {}))
            tool_instructions = "\n".join(lines)

    extra_instructions: list[str] = []
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        extra_instructions.append("The client requested JSON output. Return valid JSON only.")

    token_limit = max_completion_tokens or max_tokens
    if token_limit:
        extra_instructions.append(
            f"Keep the response under approximately {token_limit} tokens."
        )

    return "\n\n".join(part for part in [tool_instructions, *extra_instructions] if part)


def _config_block(model_name: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "config",
        "value": {
            "type": get_thread_type(model_name),
            "model": get_notion_model(model_name),
            "modelFromUser": True,
            "useWebSearch": False,
        },
    }


def _context_block(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "context",
        "value": {
            "timezone": "Asia/Shanghai",
            "currentDatetime": datetime.now().astimezone().isoformat(),
            "userId": account.get("user_id", ""),
            "spaceId": account.get("space_id", ""),
        },
    }


def _user_block(content: str, account: dict[str, Any] | None = None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "type": "user",
        "value": [[content]],
    }
    if account:
        block["userId"] = account.get("user_id", "")
        block["createdAt"] = datetime.now().astimezone().isoformat()
    return block


def _assistant_block(content: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "agent-inference",
        "value": [{"type": "text", "content": content}],
    }


def _message_role(msg: Any) -> str:
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _message_content(msg: Any) -> Any:
    if isinstance(msg, dict):
        return msg.get("content")
    return getattr(msg, "content", None)


def _message_has_tool_calls(msg: Any) -> bool:
    if isinstance(msg, dict):
        return bool(msg.get("tool_calls") or msg.get("function_call"))
    return bool(getattr(msg, "tool_calls", None) or getattr(msg, "function_call", None))


def _clean_user_text(text: str) -> str:
    return str(text or "").strip()


def build_agent_transcript(
    messages: list[Any],
    model_name: str,
    account: dict[str, Any],
    tools: Iterable[Any] | None = None,
    tool_choice: Any = None,
    response_format: dict[str, Any] | None = None,
    max_tokens: int | None = None,
    max_completion_tokens: int | None = None,
) -> list[dict[str, Any]]:
    transcript = [_config_block(model_name), _context_block(account)]

    user_texts: list[str] = []
    tool_result_texts: list[str] = []
    assistant_texts: list[str] = []

    for msg in messages:
        role = _message_role(msg)

        if role == "system":
            continue

        if role == "user":
            text = _clean_user_text(content_to_text(_message_content(msg)))
            if text:
                user_texts.append(text)
            continue

        if role in ("tool", "function"):
            tool_result_texts.append(tool_result_to_text(msg))
            continue

        if role == "assistant":
            # Tool-call messages are protocol metadata, not natural-language
            # context for Notion. Sending them as agent-inference causes echo loops.
            if _message_has_tool_calls(msg):
                continue

            text = content_to_text(_message_content(msg)).strip()
            if text and not text.startswith("[Assistant requested tool calls]"):
                assistant_texts.append(text)
            continue

    if tool_result_texts:
        original_request = user_texts[0] if user_texts else ""
        latest_user_message = user_texts[-1] if user_texts else ""
        tool_result_blob = "\n\n".join(tool_result_texts)
        if len(tool_result_blob) > 12000:
            tool_result_blob = (
                tool_result_blob[:12000]
                + "\n\n[Tool result truncated because it was too large.]"
            )
        prompt = (
            "A tool was executed by the application. Use the tool result below to answer the user's request.\n\n"
            "Original user request:\n"
            f"{original_request}\n\n"
            "Latest user message:\n"
            f"{latest_user_message}\n\n"
            "Tool result:\n"
            + tool_result_blob
            + "\n\n"
            "Now answer the user's request directly. "
            "Do not repeat the tool-call metadata. "
            "Do not request the same tool again unless the tool result is insufficient."
        )
        transcript.append(_user_block(prompt, account))
        return transcript

    latest_user_message = user_texts[-1] if user_texts else ""
    if latest_user_message:
        transcript.append(_user_block(latest_user_message, account))

    return transcript


def parse_tool_calls(
    text: str,
    allowed_tool_names: set[str],
    *,
    parallel_tool_calls: bool | None = None,
) -> list[dict[str, Any]]:
    raw_items: list[Any] = []

    multi = _MULTI_RE.search(text or "")
    if multi:
        payload = json.loads(multi.group(1))
        if isinstance(payload, list):
            raw_items.extend(payload)

    single = _SINGLE_RE.search(text or "")
    if single:
        payload = json.loads(single.group(1))
        if isinstance(payload, dict):
            raw_items.append(payload)

    calls: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        args = item.get("arguments", {})

        if name not in allowed_tool_names:
            continue

        if isinstance(args, str):
            try:
                parsed_args = json.loads(args)
            except Exception:
                parsed_args = {"input": args}
        elif isinstance(args, dict):
            parsed_args = args
        else:
            parsed_args = {"input": args}

        calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(parsed_args, ensure_ascii=False),
                },
            }
        )
        if parallel_tool_calls is False:
            break

    return calls


def apply_stop_sequences(text: str, stop: Any) -> str:
    if not stop:
        return text

    stops = [stop] if isinstance(stop, str) else stop
    cut = len(text)
    for item in stops:
        if not item:
            continue
        idx = text.find(str(item))
        if idx != -1:
            cut = min(cut, idx)
    return text[:cut]


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def build_text_json_response(response_id: str, model: str, text: str) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def build_tool_call_json_response(
    response_id: str, model: str, tool_calls: list[dict[str, Any]]
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
                    "tool_calls": tool_calls,
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def stream_text_response(response_id: str, model: str, text: str):
    yield sse(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
    )
    if text:
        yield sse(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": None}
                ],
            }
        )
    yield sse(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    yield "data: [DONE]\n\n"


def stream_tool_call_response(
    response_id: str, model: str, tool_calls: list[dict[str, Any]]
):
    yield sse(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
            ],
        }
    )

    for index, call in enumerate(tool_calls):
        yield sse(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": call["id"],
                                    "type": "function",
                                    "function": {
                                        "name": call["function"]["name"],
                                        "arguments": call["function"]["arguments"],
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )

    yield sse(
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        }
    )
    yield "data: [DONE]\n\n"
