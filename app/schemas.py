import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

# ================================
# 请求相关 Schema (Chat Completion)
# ================================

class ChatMessage(BaseModel):
    """单条对话消息：请求侧保持宽松，避免代理层过早 422。"""
    role: str
    content: Any = None
    thinking: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    function_call: Any = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    class Config:
        extra = "allow"

class ChatCompletionRequest(BaseModel):
    """
    OpenAI-Compatible 发起完成请求的 Payload。
    保留 `conversation_id` 作为特定的扩展字段，若缺失则视为独立请求。
    """
    model: str = Field(default="claude-opus4.6", description="Requested model.")
    messages: List[ChatMessage]
    stream: bool = Field(default=False, description="Whether to stream the response as SSE.")
    temperature: Optional[float] = Field(default=None, description="Sampling temperature.")
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stop: Any = None
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Any = None
    parallel_tool_calls: Optional[bool] = None
    response_format: Any = None
    conversation_id: Optional[str] = Field(default=None, description="Extension for stateful conversation tracking.")

    class Config:
        extra = "allow"

# ================================
# 非流式返回 Schema
# ================================

class ChatMessageResponseChoice(BaseModel):
    """非流式响应的选项"""
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class ChatCompletionResponse(BaseModel):
    """
    OpenAI-Compatible 完整返回 Payload。
    """
    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatMessageResponseChoice]
    usage: Dict[str, int] = Field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    # Standard 模式扩展字段
    search_metadata: Optional[Dict[str, Any]] = Field(default=None)

# ================================
# 流式返回 Schema (供内部组织)
# ================================

class ChatCompletionChunkDelta(BaseModel):
    """SSE Delta Block"""
    content: Optional[str] = None
    role: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

class ChatCompletionChunkChoice(BaseModel):
    """SSE Choice Block"""
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None

class ChatCompletionChunk(BaseModel):
    """
    OpenAI-Compatible 流式 Chunk
    """
    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChunkChoice]
