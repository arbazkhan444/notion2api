import json
import logging
import os
from datetime import datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):
        value = {'timestamp': datetime.fromtimestamp(record.created).isoformat(),
                 'level': record.levelname, 'message': record.getMessage()}
        # Explicit metadata allowlist: no prompts, tool arguments/results, bodies,
        # cookies, upstream excerpts, or raw protocol lines in routine logs.
        allowed = {'event', 'method', 'path', 'status_code', 'status', 'duration_ms', 'mode',
                   'accounts', 'attempt', 'max_retries', 'retriable', 'exception_type',
                   'message_count', 'row_count', 'summary_count', 'round_number',
                   'content_length', 'thinking_length', 'wait_seconds', 'cooldown_seconds'}
        info = getattr(record, 'request_info', {})
        if isinstance(info, dict):
            value.update({k: v for k, v in info.items() if k in allowed})
        if record.exc_info:
            value['exception_type'] = record.exc_info[0].__name__
        return json.dumps(value, ensure_ascii=False, default=str)


def setup_logger(name='notion_opus'):
    logger = logging.getLogger(name)
    logger.setLevel(os.getenv('LOG_LEVEL', 'INFO').upper())
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    logger.propagate = False
    return logger


logger = setup_logger()
