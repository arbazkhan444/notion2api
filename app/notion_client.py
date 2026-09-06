import math
import os
import threading
import time
import uuid
from email.utils import parsedate_to_datetime

import cloudscraper
import requests
from app.model_registry import get_notion_model, get_thread_type
from app.stream_parser import parse_stream

NOTION_CLIENT_VERSION = os.getenv('NOTION_CLIENT_VERSION', '23.13.20260228.0625')


class NotionUpstreamError(RuntimeError):
    def __init__(self, message, *, status_code=None, retriable=True, response_excerpt='', retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retriable = retriable
        self.response_excerpt = response_excerpt
        self.retry_after = retry_after


def _retry_after_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = 60.0
    return max(1.0, seconds) if math.isfinite(seconds) else 60.0


class NotionOpusAPI:
    def __init__(self, account_config):
        self.token_v2 = account_config.get('token_v2', '')
        self.space_id = account_config.get('space_id', '')
        self.user_id = account_config.get('user_id', '')
        self.space_view_id = account_config.get('space_view_id', '')
        self.user_name = account_config.get('user_name', 'user')
        self.user_email = account_config.get('user_email', '')
        self.cookies = dict(account_config.get('cookies') or {})
        self.cookies['token_v2'] = self.token_v2
        self.url = 'https://www.notion.so/api/v3/runInferenceTranscript'
        self.delete_url = 'https://www.notion.so/api/v3/saveTransactions'
        self.account_key = self.user_email or self.user_id or 'unknown-account'
        self._scraper = cloudscraper.create_scraper()
        self._scraper_lock = threading.Lock()

    def _build_cookie_header(self):
        cookies = {**self.cookies, 'notion_user_id': self.user_id}
        return '; '.join(f'{name}={value}' for name, value in cookies.items() if value)

    def _to_notion_transcript(self, transcript):
        converted = []
        for block in transcript:
            if block.get('type') != 'config' or not isinstance(block.get('value'), dict):
                converted.append(block)
                continue
            value = dict(block['value'])
            value['model'] = get_notion_model(str(value.get('model') or ''))
            value['type'] = get_thread_type(value['model'])
            value['useReadOnlyMode'] = True
            converted.append({**block, 'value': value})
        return converted

    def _resolve_thread_type(self, transcript):
        return next((block['value'].get('type', 'workflow') for block in transcript
                     if block.get('type') == 'config' and isinstance(block.get('value'), dict)), 'workflow')

    def _resolve_request_profile(self, thread_type, *, agent_mode=False):
        markdown = thread_type == 'markdown-chat'
        return {'thread_type': thread_type, 'create_thread': not markdown,
                'is_partial_transcript': markdown and not agent_mode,
                'precreate_thread': markdown and not agent_mode,
                'include_debug_overrides': not agent_mode, 'generate_title': not agent_mode,
                'save_all_thread_operations': not agent_mode, 'set_unread_state': not agent_mode}

    def _build_thread_headers(self):
        return {'content-type': 'application/json', 'cookie': self._build_cookie_header(),
                'x-notion-active-user-header': self.user_id, 'x-notion-space-id': self.space_id}

    def _transaction(self, thread_id, command, args):
        return {'requestId': str(uuid.uuid4()), 'transactions': [{'id': str(uuid.uuid4()), 'spaceId': self.space_id,
            'operations': [{'pointer': {'table': 'thread', 'id': thread_id, 'spaceId': self.space_id},
                            'path': [], 'command': command, 'args': args}]}]}

    def _create_thread(self, thread_id, thread_type):
        now = int(time.time() * 1000)
        payload = self._transaction(thread_id, 'set', {'id': thread_id, 'version': 1,
            'parent_id': self.space_id, 'parent_table': 'space', 'space_id': self.space_id,
            'created_time': now, 'created_by_id': self.user_id, 'created_by_table': 'notion_user',
            'messages': [], 'data': {}, 'alive': True, 'type': thread_type})
        try:
            with requests.post(self.delete_url, json=payload, headers=self._build_thread_headers(), timeout=20) as response:
                return response.status_code == 200
        except requests.RequestException:
            return False

    def delete_thread(self, thread_id):
        payload = self._transaction(thread_id, 'update', {'alive': False})
        with requests.post(self.delete_url, json=payload, headers=self._build_thread_headers(), timeout=15) as response:
            if response.status_code != 200:
                raise NotionUpstreamError('Upstream thread deletion failed.', status_code=response.status_code, retriable=False)

    def stream_response(self, transcript, thread_id=None, *, agent_mode=False, new_thread=False):
        if not isinstance(transcript, list) or not transcript:
            raise ValueError('Transcript must be a nonempty list.')
        transcript = self._to_notion_transcript(transcript)
        thread_type = self._resolve_thread_type(transcript)
        profile = self._resolve_request_profile(thread_type, agent_mode=agent_mode)
        create = thread_id is None or new_thread
        thread_id = thread_id or str(uuid.uuid4())
        # The caller supplies its own request-specific ID; it must not read this
        # compatibility attribute to identify another concurrent request.
        self.current_thread_id = thread_id
        if agent_mode and create:
            profile.update(create_thread=True, is_partial_transcript=False, precreate_thread=False)
        elif profile['precreate_thread'] and create:
            if not self._create_thread(thread_id, thread_type):
                profile.update(create_thread=True, is_partial_transcript=False)
        elif not create:
            profile.update(create_thread=False, is_partial_transcript=True)
        headers = {
            'Content-Type': 'application/json', 'Accept': 'application/x-ndjson',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36',
            'x-notion-space-id': self.space_id, 'x-notion-active-user-header': self.user_id,
            'notion-audit-log-platform': 'web', 'notion-client-version': NOTION_CLIENT_VERSION,
            'origin': 'https://www.notion.so', 'referer': 'https://www.notion.so/ai', 'cookie': self._build_cookie_header()}
        payload = {
            'traceId': str(uuid.uuid4()), 'spaceId': self.space_id, 'threadId': thread_id,
            'threadType': thread_type, 'createThread': profile['create_thread'],
            'generateTitle': profile['generate_title'], 'saveAllThreadOperations': profile['save_all_thread_operations'],
            'setUnreadState': profile['set_unread_state'], 'isPartialTranscript': profile['is_partial_transcript'],
            'asPatchResponse': True, 'isUserInAnySalesAssistedSpace': False, 'isSpaceSalesAssisted': False,
            'threadParentPointer': {'table': 'space', 'id': self.space_id, 'spaceId': self.space_id}, 'transcript': transcript}
        if profile['include_debug_overrides']:
            payload['debugOverrides'] = {'emitAgentSearchExtractedResults': True, 'cachedInferences': {},
                                         'annotationInferences': {}, 'emitInferences': False}
        response = None
        try:
            # requests.Session cookie mutation is not safe to share across posts.
            with self._scraper_lock:
                self._scraper.cookies.clear()
                response = self._scraper.post(self.url, headers=headers, json=payload, stream=True, timeout=(15, 120))
            if response.status_code != 200:
                raise NotionUpstreamError(f'Notion returned HTTP {response.status_code}.',
                    status_code=response.status_code, retriable=response.status_code >= 500 or response.status_code == 429,
                    retry_after=_retry_after_seconds(response.headers.get('Retry-After')) if response.status_code == 429 else None)
            emitted = False
            for event in parse_stream(response, initial_transcript=transcript):
                emitted = True
                yield event
            if not emitted:
                raise NotionUpstreamError('Empty upstream stream.', status_code=502)
        except requests.Timeout as exc:
            raise NotionUpstreamError('Notion request timed out.') from exc
        except requests.RequestException as exc:
            raise NotionUpstreamError('Notion transport or stream failed.') from exc
        finally:
            if response is not None:
                response.close()
