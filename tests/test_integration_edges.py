"""Extra integration cases; fake accounts and no outbound HTTP."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.update(API_KEY='test-only-key-not-a-secret', APP_MODE='heavy',
    NOTION_ACCOUNTS='[{"token_v2":"test-placeholder","user_id":"test-user","space_id":"test-space"}]',
    REQUESTS_PER_MINUTE='1000', SILICONFLOW_API_KEY='')

from fastapi.testclient import TestClient
from requests.exceptions import ChunkedEncodingError
from app import memory_store
from app.notion_client import NotionOpusAPI
from app.request_guard import RequestGuard
from app.stream_parser import parse_stream


class IntegrationEdges(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DB_PATH': self.directory.name + '/db.sqlite'})
        self.env.start()
        self.network = patch('requests.sessions.Session.request', side_effect=AssertionError('Live HTTP forbidden'))
        self.summarizer = patch('httpx.AsyncClient.post', side_effect=AssertionError('Live summarizer forbidden'))
        self.network.start()
        self.summarizer.start()
        from app.server import app
        self.app = app
        self.client = TestClient(app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer test-only-key-not-a-secret'}
        self.manager = app.state.conversation_manager

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.summarizer.stop()
        self.network.stop()
        self.env.stop()
        self.directory.cleanup()

    def post(self, **extra):
        return self.client.post('/v1/chat/completions', headers=self.headers,
            json={'messages': [{'role': 'user', 'content': 'hello'}], **extra})

    def test_both_browser_entrypoints_serve_the_safety_layer(self):
        for path in ('/', '/index.html'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('src="/safe-browser.js"', response.text)
            self.assertIn('src="/app-fixes.js"', response.text)
            self.assertIn('SafeBrowserStorage.loadChats()', response.text)
            self.assertNotIn("JSON.parse(localStorage.getItem('claude_chats'))", response.text)
            self.assertLess(response.text.index('src="/safe-browser.js"'), response.text.index('/* ===== NAMESPACE ===== */'))

    def test_zero_limits_are_rejected_not_replaced_by_defaults(self):
        for option in ('max_concurrent', 'max_body', 'per_minute'):
            with self.assertRaises(ValueError):
                RequestGuard(None, **{option: 0})

    def test_old_counter_cannot_reuse_an_archived_round(self):
        conversation = self.manager.new_conversation()
        self.manager.persist_round(conversation, 'first', 'answer')
        with self.manager._get_conn() as conn:
            conn.execute('UPDATE conversations SET next_round_index=0 WHERE id=?', (conversation,))
        self.assertEqual(self.manager.persist_round(conversation, 'second', 'answer'), 1)

    def test_ambiguous_legacy_archive_is_not_silently_reconstructed(self):
        conversation = self.manager.new_conversation()
        self.manager.persist_round(conversation, 'first', 'answer')
        with self.manager._get_conn() as conn:
            self.manager._archive_message(conn, conversation, 'user', 'conflicting', 0, 1)
        with self.assertRaisesRegex(ValueError, 'conflicting turns'):
            memory_store.ensure_window(self.manager, conversation)
        self.assertEqual(self.post(conversation_id=conversation).status_code, 409)

    def test_stop_at_start_can_return_empty_success(self):
        def answer(*args, **kwargs):
            yield {'type': 'content', 'text': 'END trailing text'}
        with patch.object(NotionOpusAPI, 'stream_response', answer):
            response = self.post(stop='END')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['choices'][0]['message']['content'], '')

    def test_json_format_is_sent_to_upstream_not_only_validated_afterward(self):
        transcripts = []
        def answer(client, transcript, **kwargs):
            transcripts.append(transcript)
            yield {'type': 'content', 'text': '{"ok":true}'}
        with patch.object(NotionOpusAPI, 'stream_response', answer):
            response = self.post(response_format={'type': 'json_object'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('Return JSON only', json.dumps(transcripts))

    def test_normal_stream_does_not_publish_retracted_reasoning(self):
        def answer(*args, **kwargs):
            yield {'type': 'thinking', 'text': 'retracted-reasoning'}
            yield {'type': 'thinking_replace', 'text': 'final-reasoning'}
            yield {'type': 'final_thinking', 'text': 'final-reasoning'}
            yield {'type': 'content', 'text': 'answer'}
        with patch.object(NotionOpusAPI, 'stream_response', answer):
            response = self.post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('retracted-reasoning', response.text)
        self.assertIn('final-reasoning', response.text)
        self.assertIn('"finish_reason": "stop"', response.text)

    def test_protocol_negative_array_indices_fail(self):
        patches = [{'o': 'a', 'path': '/s/-', 'v': {'type': 'text', 'value': 'answer'}},
                   {'o': 'p', 'path': '/s/-1', 'v': {'type': 'text', 'value': 'bad'}}]
        response = SimpleNamespace(iter_lines=lambda **kwargs: iter([json.dumps({'type': 'patch', 'v': patches})]))
        with self.assertRaises(ChunkedEncodingError):
            list(parse_stream(response))

    def test_insert_before_initial_history_does_not_reemit_it(self):
        initial = [{'id': 'old-step', 'type': 'agent-inference', 'value': [{'type': 'text', 'content': 'old-history'}]}]
        patches = [{'o': 'a', 'path': '/s/0', 'v': {'type': 'text', 'value': 'new-answer'}}]
        response = SimpleNamespace(iter_lines=lambda **kwargs: iter([json.dumps({'type': 'patch', 'v': patches})]))
        events = list(parse_stream(response, initial))
        self.assertEqual(events[-1]['text'], 'new-answer')
        self.assertNotIn('old-history', json.dumps(events))


if __name__ == '__main__':
    unittest.main()
