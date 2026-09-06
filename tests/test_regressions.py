"""Actual application tests. All credentials and transports are fake."""
import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.update(API_KEY='test-only-key-not-a-secret',
                  NOTION_ACCOUNTS='[{"token_v2":"test-placeholder","user_id":"test-user","space_id":"test-space"}]',
                  APP_MODE='heavy', REQUESTS_PER_MINUTE='1000', SILICONFLOW_API_KEY='')

from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from requests.exceptions import ChunkedEncodingError

from app import memory_store
from app.agent_adapter import build_agent_transcript, parse_tool_calls
from app.conversation import ConversationManager, build_lite_transcript, build_standard_transcript
from app.model_registry import MODEL_MAP, get_notion_model, get_thread_type
from app.notion_client import NotionOpusAPI, NotionUpstreamError
from app.proxy_contracts import ContractError, validate_calls
from app.request_guard import RequestGuard
from app.stream_parser import parse_stream, _strip_primary_attr_fragments, _clean_extracted_text

TOOLS = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {
    'type': 'object', 'properties': {'path': {'type': 'string'}}, 'required': ['path'], 'additionalProperties': False}}}]


def setUpModule():
    # Fail closed if an unmocked production transport is accidentally exercised.
    global network_patches
    network_patches = [patch('requests.sessions.Session.request', side_effect=AssertionError('Live HTTP forbidden in tests')),
                       patch('httpx.AsyncClient.post', side_effect=AssertionError('Live summarizer forbidden in tests'))]
    for item in network_patches:
        item.start()


def tearDownModule():
    for item in network_patches:
        item.stop()


class ContractTests(unittest.TestCase):
    def test_every_model_is_idempotent_through_real_transport(self):
        for model, expected in MODEL_MAP.items():
            with self.subTest(model=model):
                self.assertEqual(get_notion_model(get_notion_model(model)), expected)
                for transcript in [build_lite_transcript('hi', model),
                                   build_standard_transcript([{'role': 'user', 'content': 'hi'}], model, {}),
                                   build_agent_transcript([{'role': 'user', 'content': 'hi'}], model, {}, TOOLS)]:
                    outgoing = NotionOpusAPI._to_notion_transcript(None, transcript)
                    self.assertEqual(outgoing[0]['value']['model'], expected)
                    self.assertEqual(outgoing[0]['value']['type'], get_thread_type(model))
                    self.assertTrue(outgoing[0]['value']['useReadOnlyMode'])

    def test_unknown_model_is_not_silently_defaulted(self):
        with self.assertRaises(ValueError):
            get_notion_model('not-a-model')

    def test_standard_preserves_dialogue_order(self):
        messages = [{'role': r, 'content': c} for r, c in [('user', 'u1'), ('assistant', 'a1'), ('user', 'u2')]]
        transcript = build_standard_transcript(messages, next(iter(MODEL_MAP)), {})
        self.assertEqual([b['type'] for b in transcript[2:]], ['user', 'agent-inference', 'user'])
        text = json.dumps(transcript)
        self.assertLess(text.index('u1'), text.index('a1'))
        self.assertLess(text.index('a1'), text.index('u2'))

    def test_agent_preserves_instructions_calls_and_latest_result(self):
        messages = [{'role': 'system', 'content': 'SYSTEM_SENTINEL cline'},
                    {'role': 'developer', 'content': 'DEVELOPER_SENTINEL'},
                    {'role': 'user', 'content': 'FIRST_USER_SENTINEL'},
                    {'role': 'assistant', 'content': 'ASSISTANT_SENTINEL',
                     'tool_calls': [{'id': 'call_one', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"old"}'}}]},
                    {'role': 'tool', 'tool_call_id': 'call_one', 'content': 'x' * 13000},
                    {'role': 'user', 'content': 'SECOND_USER_SENTINEL'},
                    {'role': 'tool', 'tool_call_id': 'call_two', 'content': 'LATEST_RESULT_SENTINEL'}]
        text = json.dumps(build_agent_transcript(messages, next(iter(MODEL_MAP)), {}, TOOLS))
        for sentinel in ('SYSTEM_SENTINEL', 'DEVELOPER_SENTINEL', 'FIRST_USER_SENTINEL',
                         'ASSISTANT_SENTINEL', 'SECOND_USER_SENTINEL', 'LATEST_RESULT_SENTINEL', 'call_one', 'call_two', 'additionalProperties'):
            self.assertIn(sentinel, text)

    def test_multiple_single_call_envelopes_are_all_parsed(self):
        tag = '<openai_tool_call>{"name":"read_file","arguments":{"path":"a"}}</openai_tool_call>'
        self.assertEqual(len(parse_tool_calls(tag + tag, {'read_file'})), 2)
        with self.assertRaises(ContractError):
            parse_tool_calls(tag + tag, {'read_file'}, parallel_tool_calls=False)

    def test_invalid_arguments_fail_instead_of_being_coerced(self):
        for args in ('[]', '"[]"', 'null', '"not json"'):
            text = '<openai_tool_call>{"name":"read_file","arguments":' + args + '}</openai_tool_call>'
            with self.subTest(args=args), self.assertRaises(ContractError):
                parse_tool_calls(text, {'read_file'})

    def test_unknown_or_incomplete_tool_calls_fail(self):
        for text in ('<openai_tool_call>{', '<openai_tool_call>{"name":"delete_all"}</openai_tool_call>'):
            with self.assertRaises(ContractError):
                parse_tool_calls(text, {'read_file'})

    def test_tool_policy_and_argument_schema(self):
        calls = parse_tool_calls('<openai_tool_call>{"name":"read_file","arguments":{"path":"a"}}</openai_tool_call>', {'read_file'})
        validate_calls(calls, TOOLS, 'required')
        with self.assertRaises(ContractError):
            validate_calls(calls, TOOLS, 'none')
        with self.assertRaises(ContractError):
            validate_calls([], TOOLS, 'required')
        invalid = [{'function': {'name': 'read_file', 'arguments': '{"path":12}'}}]
        with self.assertRaises(ContractError):
            validate_calls(invalid, TOOLS)

    def test_named_tool_choice_is_enforced(self):
        tools = TOOLS + [{'type': 'function', 'function': {'name': 'other', 'parameters': {'type': 'object'}}}]
        calls = [{'function': {'name': 'other', 'arguments': '{}'}}]
        with self.assertRaises(ContractError):
            validate_calls(calls, tools, {'type': 'function', 'function': {'name': 'read_file'}})

    def test_remote_schema_references_are_not_fetched(self):
        tools = [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'$ref': 'https://example.invalid/private'}}}]
        calls = [{'function': {'name': 'read_file', 'arguments': '{}'}}]
        with self.assertRaises(ContractError):
            validate_calls(calls, tools)

    def test_direct_router_never_fabricates_calls(self):
        from app.cline_tool_router import build_direct_tool_call_response
        req = SimpleNamespace(tools=TOOLS, tool_choice='none', messages=[{'role': 'user', 'content': 'Read package.json'}])
        self.assertIsNone(build_direct_tool_call_response(req, next(iter(MODEL_MAP))))


class ParserTests(unittest.TestCase):
    def parse(self, patches, initial=None):
        response = SimpleNamespace(iter_lines=lambda **kw: iter([json.dumps({'type': 'patch', 'v': patches})]))
        return list(parse_stream(response, initial_transcript=initial))

    def test_primary_chunk_boundary_and_code_are_preserved(self):
        state = [False]
        self.assertEqual(''.join(_strip_primary_attr_fragments(x, state) for x in ['The primary', ' key is important.']), 'The primary key is important.')
        self.assertEqual(_clean_extracted_text('primary="en"'), 'primary="en"')
        self.assertEqual(_clean_extracted_text('```html\n<lang primary="en">code</lang>\n```'), '```html\n<lang primary="en">code</lang>\n```')

    def test_complete_outer_language_wrapper(self):
        self.assertEqual(_clean_extracted_text('<lang primary="en">hello</lang>'), 'hello')

    def test_legitimate_json_is_answer_not_search(self):
        value = '{"questions":["Why?"],"sources":[]}'
        events = self.parse([{'o': 'a', 'path': '/s/-', 'v': {'type': 'text', 'value': [{'type': 'text', 'content': value}]}}])
        self.assertFalse(any(e['type'] == 'search' for e in events))
        self.assertEqual(events[-1]['text'], value)

    def test_append_then_replacement_is_not_concatenated(self):
        events = self.parse([{'o': 'a', 'path': '/s/-', 'v': {'type': 'text', 'value': [{'type': 'text', 'content': 'Hel'}]}},
                             {'o': 'p', 'path': '/s/0/value/0/content', 'v': 'Hello'}])
        self.assertEqual(events[-1]['text'], 'Hello')
        self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'content'), 'Hello')

    def test_divergent_replacement_preserves_other_blocks(self):
        events = self.parse([{'o': 'a', 'path': '/s/-', 'v': {'type': 'text', 'value': [{'type': 'text', 'content': 'old'}, {'type': 'text', 'content': ' tail'}]}},
                             {'o': 'p', 'path': '/s/0/value/0/content', 'v': 'new'}])
        self.assertEqual(events[-1]['text'], 'new tail')

    def test_initial_transcript_is_not_reemitted(self):
        initial = [{'type': 'user', 'value': [['secret history']]}]
        events = self.parse([{'o': 'a', 'path': '/s/-', 'v': {'type': 'text', 'value': [{'type': 'text', 'content': 'a'}]}},
                             {'o': 'x', 'path': '/s/1/value/0/content', 'v': 'b'}], initial)
        self.assertEqual(events[-1]['text'], 'ab')
        self.assertNotIn('secret history', json.dumps(events))

    def test_unknown_patch_path_fails_closed(self):
        with self.assertRaises(ChunkedEncodingError):
            self.parse([{'o': 'x', 'path': '/s/99/value/0/content', 'v': 'text'}])

    def test_protocol_error_is_not_a_successful_answer(self):
        for data in ([1, 2], {'type': 'error', 'error': 'failed'}):
            response = SimpleNamespace(iter_lines=lambda **kw: iter([json.dumps(data)]))
            with self.assertRaises(ChunkedEncodingError):
                list(parse_stream(response))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DB_PATH': self.directory.name + '/db.sqlite'})
        self.env.start()
        self.manager = ConversationManager()
        self.conv = self.manager.new_conversation()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def count(self, table):
        with self.manager._get_conn() as conn:
            return conn.execute('SELECT COUNT(*) FROM ' + table + ' WHERE conversation_id=?', (self.conv,)).fetchone()[0]

    def test_concurrent_rounds_do_not_overwrite(self):
        barrier = threading.Barrier(8)
        def persist(index):
            barrier.wait()
            return self.manager.persist_round(self.conv, f'u{index}', f'a{index}')
        with ThreadPoolExecutor(max_workers=8) as executor:
            indices = list(executor.map(persist, range(8)))
        self.assertEqual(sorted(indices), list(range(8)))
        self.assertEqual(self.count('messages'), 16)
        self.assertEqual(self.count('sliding_window'), 8)
        with self.manager._get_conn() as conn:
            self.assertEqual(conn.execute('SELECT next_round_index FROM conversations WHERE id=?', (self.conv,)).fetchone()[0], 8)

    def test_reimport_after_message_compaction_does_not_duplicate(self):
        history = [('user', 'u1', ''), ('assistant', 'a1', ''), ('user', 'u2', ''), ('assistant', 'a2', '')]
        memory_store.import_history(self.manager, self.conv, history)
        with self.manager._get_conn() as conn:
            conn.execute('DELETE FROM messages WHERE conversation_id=?', (self.conv,))
        memory_store.import_history(self.manager, self.conv, history)
        self.assertEqual(self.count('sliding_window'), 2)
        self.assertEqual(self.count('full_archive'), 4)
        memory_store.import_history(self.manager, self.conv, history[2:] + [('user', 'u3', ''), ('assistant', 'a3', '')])
        self.assertEqual(self.count('sliding_window'), 3)

    def test_conflicting_history_is_not_silently_appended(self):
        self.manager.persist_round(self.conv, 'u1', 'a1')
        with self.assertRaises(ValueError):
            memory_store.import_history(self.manager, self.conv, [('user', 'other', ''), ('assistant', 'other', '')])
        self.assertEqual(self.count('sliding_window'), 1)

    def test_cli_add_message_populates_window(self):
        self.manager.add_message(self.conv, 'user', 'u1')
        self.manager.add_message(self.conv, 'assistant', 'a1')
        self.assertEqual(self.count('sliding_window'), 1)

    def test_thread_owner_is_stored_with_binding(self):
        memory_store.bind_thread(self.manager, self.conv, 'fake-thread', 'fake-model', 'user:space')
        self.assertEqual(memory_store.thread_binding(self.manager, self.conv)['thread_owner'], 'user:space')

    def test_restored_history_reaches_actual_payload(self):
        memory_store.import_history(self.manager, self.conv, [('user', 'RESTORED_USER', ''), ('assistant', 'RESTORED_ANSWER', '')])
        client = SimpleNamespace(user_id='u', space_id='s', user_name='name', user_email='', space_view_id='')
        payload, degraded = memory_store.build_payload(self.manager, self.conv, 'next', client, next(iter(MODEL_MAP)))
        self.assertIn('RESTORED_USER', json.dumps(payload))
        self.assertIn('RESTORED_ANSWER', json.dumps(payload))
        self.assertFalse(degraded)


class CompressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DB_PATH': self.directory.name + '/db.sqlite'})
        self.env.start()
        self.manager = ConversationManager()
        self.conv = self.manager.new_conversation()
        self.manager.persist_round(self.conv, 'u', 'a')

    async def asyncTearDown(self):
        self.env.stop()
        self.directory.cleanup()

    async def test_cancelled_compression_retains_raw_turn(self):
        started = asyncio.Event()
        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()
        with patch('app.summarizer.is_summarizer_configured', return_value=True), patch('app.summarizer.summarize_turn', side_effect=blocked):
            task = asyncio.create_task(memory_store.compress_one(self.manager, self.conv, 0))
            await started.wait()
            with self.manager._get_conn() as conn:
                self.assertEqual(len(self.manager.get_sliding_window(conn, self.conv)), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with self.manager._get_conn() as conn:
            row = conn.execute('SELECT compress_status,compression_started_at FROM sliding_window').fetchone()
            self.assertEqual(row['compress_status'], 'active')
            self.assertIsNone(row['compression_started_at'])

    async def test_compression_is_idempotent(self):
        with patch('app.summarizer.is_summarizer_configured', return_value=True), patch('app.summarizer.summarize_turn', new=AsyncMock(return_value='summary')) as mock:
            await memory_store.compress_one(self.manager, self.conv, 0)
            await memory_store.compress_one(self.manager, self.conv, 0)
            self.assertEqual(mock.await_count, 1)
        with self.manager._get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM compressed_summaries WHERE compress_status='done'").fetchone()[0], 1)

    async def test_expired_lease_is_recovered(self):
        with self.manager._get_conn() as conn:
            conn.execute("UPDATE sliding_window SET compress_status='compressing',compression_started_at=?", (time.time() - 121,))
        with patch('app.summarizer.is_summarizer_configured', return_value=True), patch('app.summarizer.summarize_turn', new=AsyncMock(return_value='summary')):
            await memory_store.compress_one(self.manager, self.conv, 0)
        with self.manager._get_conn() as conn:
            self.assertEqual(conn.execute('SELECT compress_status FROM sliding_window').fetchone()[0], 'compressed')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'DB_PATH': self.directory.name + '/db.sqlite'})
        self.env.start()
        from app.server import app
        self.app = app
        self.client = TestClient(app)
        self.client.__enter__()
        self.headers = {'Authorization': 'Bearer test-only-key-not-a-secret'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.directory.cleanup()

    def post(self, **extra):
        return self.client.post('/v1/chat/completions', headers=self.headers,
                                json={'messages': [{'role': 'user', 'content': 'hello'}], **extra})

    def test_auth_is_enforced(self):
        result = self.client.post('/v1/chat/completions', json={'messages': []})
        self.assertEqual(result.status_code, 401)

    def test_unsupported_token_limit_is_explicit(self):
        self.assertEqual(self.post(max_tokens=12).status_code, 400)

    def test_cors_exposes_conversation_headers(self):
        result = self.client.get('/health', headers={'Origin': 'http://localhost:8000'})
        self.assertIn('X-Conversation-Id', result.headers['access-control-expose-headers'])

    def test_upstream_error_does_not_manufacture_completion_tool(self):
        def broken(*args, **kw):
            raise NotionUpstreamError('failed', status_code=503, retriable=False)
            yield
        tools = [{'type': 'function', 'function': {'name': 'attempt_completion', 'parameters': {'type': 'object'}}}]
        messages = [{'role': 'user', 'content': 'do task'}, {'role': 'tool', 'tool_call_id': 'call_fake', 'content': '{"error":"permission denied"}'}]
        with patch.object(NotionOpusAPI, 'stream_response', broken):
            result = self.post(tools=tools, messages=messages)
        self.assertGreaterEqual(result.status_code, 500)
        self.assertIn('error', result.json())
        self.assertNotIn('choices', result.json())

    def test_tool_choice_none_cannot_emit_calls(self):
        def answer(*args, **kw):
            yield {'type': 'content', 'text': '<openai_tool_call>{"name":"read_file","arguments":{"path":"a"}}</openai_tool_call>'}
        with patch.object(NotionOpusAPI, 'stream_response', answer):
            result = self.post(tools=TOOLS, tool_choice='none')
        self.assertEqual(result.status_code, 502)

    def test_stream_failure_has_error_but_no_success_finish(self):
        closed = threading.Event()
        def broken(*args, **kw):
            try:
                yield {'type': 'content', 'text': 'partial'}
                raise NotionUpstreamError('interrupted', status_code=502)
            finally:
                closed.set()
        with patch.object(NotionOpusAPI, 'stream_response', broken):
            result = self.post(stream=True)
        self.assertIn('"error"', result.text)
        self.assertNotIn('"finish_reason": "stop"', result.text)
        self.assertTrue(closed.is_set())

    def test_health_remains_responsive_during_blocking_transport(self):
        started = threading.Event()
        def slow(*args, **kw):
            started.set()
            time.sleep(0.3)
            yield {'type': 'content', 'text': 'answer'}
        with patch.object(NotionOpusAPI, 'stream_response', slow), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.post)
            self.assertTrue(started.wait(2))
            start = time.monotonic()
            self.assertEqual(self.client.get('/health').status_code, 200)
            self.assertLess(time.monotonic() - start, 0.2)
            self.assertEqual(future.result().status_code, 200)

    def test_successful_history_is_reused_on_same_account(self):
        calls = []
        def answer(client, transcript, **kw):
            calls.append((transcript, kw))
            yield {'type': 'content', 'text': 'answer'}
        with patch.object(NotionOpusAPI, 'stream_response', answer):
            first = self.post()
            conv = first.headers['x-conversation-id']
            second = self.post(conversation_id=conv, messages=[{'role': 'user', 'content': 'next'}])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(calls[0][1]['new_thread'])
        self.assertFalse(calls[1][1]['new_thread'])
        self.assertIn('hello', json.dumps(calls[1][0]))

    def test_owner_change_recreates_upstream_thread(self):
        calls = []
        def answer(client, transcript, **kw):
            calls.append(kw)
            yield {'type': 'content', 'text': 'answer'}
        with patch.object(NotionOpusAPI, 'stream_response', answer):
            first = self.post()
            conv = first.headers['x-conversation-id']
            from app.account_pool import AccountPool
            self.app.state.account_pool = AccountPool([{'token_v2': 'test-placeholder', 'user_id': 'other', 'space_id': 'other-space'}])
            second = self.post(conversation_id=conv, messages=[{'role': 'user', 'content': 'next'}])
        self.assertEqual(second.status_code, 200)
        self.assertTrue(calls[1]['new_thread'])
        self.assertNotEqual(calls[0]['thread_id'], calls[1]['thread_id'])

    def test_delete_without_preinitialized_manager_is_safe(self):
        conv = self.app.state.conversation_manager.new_conversation()
        del self.app.state.conversation_manager
        result = self.client.delete('/v1/conversations/' + conv, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['scope'], 'local_history')


class GuardTests(unittest.TestCase):
    def client(self, **limits):
        app = FastAPI()
        @app.post('/v1/chat/completions')
        async def endpoint():
            return {'ok': True}
        return TestClient(RequestGuard(app, **limits))

    def test_rate_limit_is_actually_enforced(self):
        with self.client(per_minute=2) as client:
            self.assertEqual(client.post('/v1/chat/completions', json={}).status_code, 200)
            self.assertEqual(client.post('/v1/chat/completions', json={}).status_code, 200)
            self.assertEqual(client.post('/v1/chat/completions', json={}).status_code, 429)

    def test_large_body_is_rejected(self):
        with self.client(max_body=8) as client:
            self.assertEqual(client.post('/v1/chat/completions', content='x' * 9).status_code, 413)


if __name__ == '__main__':
    unittest.main()
