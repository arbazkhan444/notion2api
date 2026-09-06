import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Deployment environment takes precedence over a local .env file.
load_dotenv(override=False)
REQUIRED_ACCOUNT_FIELDS = {'token_v2', 'space_id', 'user_id'}


def load_accounts():
    text = os.getenv('NOTION_ACCOUNTS', '').strip()
    if not text:
        file = Path(__file__).resolve().parent.parent / 'accounts.json'
        if file.is_file():
            text = file.read_text(encoding='utf-8').strip()
    if not text:
        raise ValueError('Configure NOTION_ACCOUNTS or accounts.json with at least one account.')
    try:
        accounts = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError('Account configuration must be valid JSON.') from None
    if not isinstance(accounts, list) or not accounts:
        raise ValueError('Account configuration must be a nonempty JSON array.')
    for index, account in enumerate(accounts):
        if not isinstance(account, dict):
            raise ValueError(f'Account {index} must be an object.')
        for field in REQUIRED_ACCOUNT_FIELDS:
            value = account.get(field)
            if not isinstance(value, str) or not value.strip() or '\r' in value or '\n' in value:
                raise ValueError(f'Account {index} has an invalid {field}.')
        cookies = account.get('cookies') or {}
        if not isinstance(cookies, dict) or any(not isinstance(k, str) or not isinstance(v, str) or '\r' in k + v or '\n' in k + v for k, v in cookies.items()):
            raise ValueError(f'Account {index} has invalid cookies.')
    return accounts


ACCOUNTS = load_accounts()
API_KEY = os.getenv('API_KEY', '')
SILICONFLOW_API_KEY = os.getenv('SILICONFLOW_API_KEY', '')
HOST = os.getenv('HOST', '127.0.0.1')
PORT = int(os.getenv('PORT', '8000'))
if not 1 <= PORT <= 65535:
    raise ValueError('PORT must be between 1 and 65535.')
ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv('ALLOWED_ORIGINS', f'http://localhost:{PORT},http://127.0.0.1:{PORT}').split(',') if origin.strip()]
APP_MODE = os.getenv('APP_MODE', 'heavy').lower().strip()
if APP_MODE not in ('lite', 'standard', 'heavy'):
    raise ValueError('APP_MODE must be lite, standard, or heavy.')


def is_lite_mode():
    return APP_MODE == 'lite'


def is_standard_mode():
    return APP_MODE == 'standard'


def get_default_account():
    return ACCOUNTS[0]
