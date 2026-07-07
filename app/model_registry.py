MODEL_MAP: dict[str, str] = {
    "claude-opus4.6": "avocado-froyo-medium",
    "claude-opus4.7": "apricot-sorbet-high",
    "claude-opus4.8": "ambrosia-tart-high",
    "claude-sonnet4.6": "almond-croissant-low",
    "claude-sonnet5": "angel-cake-high",
    "gemini-2.5flash": "vertex-gemini-2.5-flash",
    "gemini-3.1pro": "galette-medium-thinking",
    "gpt-5.2": "oatmeal-cookie",
    "gpt-5.4": "oval-kumquat-medium",
    "gpt-5.5": "opal-quince-medium",
    "kimi-2.6": "fireworks-kimi-k2.6",
    "grok-4.3": "xigua-mochi-medium",
    "grok-build0.1": "xinomavro-cake",
    "deepseek-v4pro": "baseten-deepseek-v4-pro",
}

NOTION_MODEL_REVERSE_MAP: dict[str, str] = {value: key for key, value in MODEL_MAP.items()}

MODEL_ALIASES: dict[str, str] = {
    "gpt-5": "claude-sonnet4.6",
    "gpt-5-codex": "claude-sonnet4.6",
    "gpt-4o": "claude-sonnet4.6",
    "gpt-4.1": "claude-sonnet4.6",
    "gpt-4": "claude-sonnet4.6",
    "gpt-4-turbo": "claude-sonnet4.6",
    "claude-sonnet-4": "claude-sonnet4.6",
    "claude-sonnet-4-6": "claude-sonnet4.6",
    "notion/claude-sonnet4.6": "claude-sonnet4.6",
}

DISPLAY_NAMES: dict[str, str] = {
    "claude-opus4.6": "Claude Opus 4.6",
    "claude-opus4.7": "Claude Opus 4.7",
    "claude-opus4.8": "Claude Opus 4.8",
    "claude-sonnet4.6": "Claude Sonnet 4.6",
    "claude-sonnet5": "Claude Sonnet 5",
    "gemini-2.5flash": "Gemini 2.5 Flash",
    "gemini-3.1pro": "Gemini 3.1 Pro",
    "gpt-5.2": "GPT-5.2",
    "gpt-5.4": "GPT-5.4",
    "gpt-5.5": "GPT-5.5",
    "kimi-2.6": "Kimi 2.6",
    "grok-4.3": "Grok 4.3",
    "grok-build0.1": "Grok Build 0.1",
    "deepseek-v4pro": "DeepSeek V4 Pro",
}

MODEL_ICONS: dict[str, str] = {
    "claude-opus4.6": "✳️",
    "claude-opus4.7": "✳️",
    "claude-opus4.8": "✳️",
    "claude-sonnet4.6": "✳️",
    "claude-sonnet5": "✳️",
    "gemini-2.5flash": "✦",
    "gemini-3.1pro": "✦",
    "gpt-5.2": "⚙",
    "gpt-5.4": "⚙",
    "gpt-5.5": "⚙",
    "kimi-2.6": "🌙",
    "grok-4.3": "⚡",
    "grok-build0.1": "⚡",
    "deepseek-v4pro": "🐋",
}

# 默认使用 Sonnet 4.6（速度和质量的最佳平衡）
DEFAULT_MODEL = "claude-sonnet4.6"


def normalize_model_name(model_name: str) -> str:
    if model_name in MODEL_MAP:
        return model_name
    return MODEL_ALIASES.get(model_name, model_name)


def get_notion_model(model_name: str) -> str:
    standard_name = normalize_model_name(model_name)
    return MODEL_MAP.get(standard_name, MODEL_MAP[DEFAULT_MODEL])


# 需要走 markdown-chat 的 Notion 内部代号（vertex- 前缀的模型）
# Gemini 3.1 Pro (galette-medium-thinking) 已改为 workflow，不再走 markdown-chat
MARKDOWN_CHAT_MODELS: set[str] = {
    "vertex-gemini-2.5-flash",
}


def is_gemini_model(model_name: str) -> bool:
    """判断是否为 Gemini 系列模型（用于 config block 构建等）"""
    standard_name = get_standard_model(model_name)
    if standard_name.startswith("gemini-"):
        return True
    notion_model = get_notion_model(standard_name)
    return notion_model.startswith("vertex-") or notion_model.startswith("galette-")


def get_thread_type(model_name: str) -> str:
    """
    根据模型确定 Notion thread type。
    只有 vertex- 前缀的模型走 markdown-chat，其余全部走 workflow。
    """
    standard_name = get_standard_model(model_name)
    notion_model = get_notion_model(standard_name)
    if notion_model in MARKDOWN_CHAT_MODELS:
        return "markdown-chat"
    return "workflow"


def get_standard_model(model_name: str) -> str:
    normalized = normalize_model_name(model_name)
    if normalized in MODEL_MAP:
        return normalized
    return NOTION_MODEL_REVERSE_MAP.get(normalized, DEFAULT_MODEL)


def list_available_models() -> list[str]:
    return list(MODEL_MAP.keys())


def is_supported_model(model_name: str) -> bool:
    return normalize_model_name(model_name) in MODEL_MAP


def get_display_name(model_name: str) -> str:
    standard_name = get_standard_model(model_name)
    return DISPLAY_NAMES.get(standard_name, standard_name)


def get_model_icon(model_name: str) -> str:
    standard_name = get_standard_model(model_name)
    return MODEL_ICONS.get(standard_name, "")
