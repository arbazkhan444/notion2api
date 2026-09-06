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
NOTION_MODEL_REVERSE_MAP = {value: key for key, value in MODEL_MAP.items()}
MODEL_ALIASES = {
    'gpt-5': 'claude-sonnet4.6', 'gpt-5-codex': 'claude-sonnet4.6',
    'gpt-4o': 'claude-sonnet4.6', 'gpt-4.1': 'claude-sonnet4.6',
    'gpt-4': 'claude-sonnet4.6', 'gpt-4-turbo': 'claude-sonnet4.6',
    'claude-sonnet-4': 'claude-sonnet4.6', 'claude-sonnet-4-6': 'claude-sonnet4.6',
    'notion/claude-sonnet4.6': 'claude-sonnet4.6',
}
DISPLAY_NAMES = {
    'claude-opus4.6': 'Claude Opus 4.6', 'claude-opus4.7': 'Claude Opus 4.7',
    'claude-opus4.8': 'Claude Opus 4.8', 'claude-sonnet4.6': 'Claude Sonnet 4.6',
    'claude-sonnet5': 'Claude Sonnet 5', 'gemini-2.5flash': 'Gemini 2.5 Flash',
    'gemini-3.1pro': 'Gemini 3.1 Pro', 'gpt-5.2': 'GPT-5.2', 'gpt-5.4': 'GPT-5.4',
    'gpt-5.5': 'GPT-5.5', 'kimi-2.6': 'Kimi 2.6', 'grok-4.3': 'Grok 4.3',
    'grok-build0.1': 'Grok Build 0.1', 'deepseek-v4pro': 'DeepSeek V4 Pro',
}
MODEL_ICONS = {name: ('✳️' if name.startswith('claude-') else '✦' if name.startswith('gemini-')
                     else '⚙' if name.startswith('gpt-') else '🌙' if name.startswith('kimi-')
                     else '⚡' if name.startswith('grok-') else '🐋') for name in MODEL_MAP}
DEFAULT_MODEL = 'claude-sonnet4.6'
MARKDOWN_CHAT_MODELS = {'vertex-gemini-2.5-flash'}


def normalize_model_name(model_name):
    return MODEL_ALIASES.get(model_name, model_name)


def get_notion_model(model_name):
    if model_name in NOTION_MODEL_REVERSE_MAP:
        return model_name
    name = normalize_model_name(model_name)
    if name not in MODEL_MAP:
        raise ValueError('Unsupported model.')
    return MODEL_MAP[name]


def get_standard_model(model_name):
    normalized = normalize_model_name(model_name)
    if normalized in MODEL_MAP:
        return normalized
    return NOTION_MODEL_REVERSE_MAP.get(normalized, DEFAULT_MODEL)


def is_gemini_model(model_name):
    return get_standard_model(model_name).startswith('gemini-')


def get_thread_type(model_name):
    return 'markdown-chat' if get_notion_model(model_name) in MARKDOWN_CHAT_MODELS else 'workflow'


def list_available_models():
    return list(MODEL_MAP)


def is_supported_model(model_name):
    return normalize_model_name(model_name) in MODEL_MAP


def get_display_name(model_name):
    name = get_standard_model(model_name)
    return DISPLAY_NAMES.get(name, name)


def get_model_icon(model_name):
    return MODEL_ICONS.get(get_standard_model(model_name), '')
