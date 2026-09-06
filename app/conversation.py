"""SQLite conversation facade shared by the HTTP service and terminal client.

Storage/compression implementations live in memory_store. Existing archive data
is retained; migrations add fields and mark duplicate summaries as superseded.
"""
from __future__ import annotations
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from app.agent_adapter import _config_block, _context_block, _user_block, _assistant_block
from app.model_registry import get_thread_type


class ConversationManager:
    WINDOW_SIZE = 16
    WINDOW_ROUNDS = 8
    SUMMARY_INJECT_LIMIT = 15
    RECALL_LIMIT = 5
    ASSISTANT_EMPTY_PLACEHOLDER = '[assistant_no_visible_content]'

    def __init__(self):
        self.db_path = os.getenv('DB_PATH', './data/conversations.db')
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        self._init_db()

    @contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_column(self, conn, table, column_sql):
        names = {row['name'] for row in conn.execute(f'PRAGMA table_info({table})')}
        if column_sql.split()[0] not in names:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column_sql}')

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, title TEXT, created_at INTEGER, summary TEXT,
                    next_round_index INTEGER DEFAULT 0, compress_failed_at INTEGER,
                    thread_id TEXT, thread_model TEXT);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT,
                    role TEXT, content TEXT, created_at INTEGER, thinking TEXT,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
                CREATE TABLE IF NOT EXISTS full_archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL, content TEXT NOT NULL, round_index INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_full_archive_unique
                    ON full_archive(conversation_id,round_index,role,content);
                CREATE TABLE IF NOT EXISTS sliding_window (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    round_number INTEGER NOT NULL, user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL, assistant_thinking TEXT DEFAULT '',
                    compress_status TEXT DEFAULT 'active', created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_sliding_window_unique
                    ON sliding_window(conversation_id,round_number);
                CREATE TABLE IF NOT EXISTS compressed_summaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,
                    round_index INTEGER NOT NULL, user_content TEXT NOT NULL,
                    assistant_content TEXT NOT NULL, summary TEXT,
                    compress_status TEXT DEFAULT 'pending', created_at INTEGER NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE);
            ''')
            for column in ('summary TEXT', 'next_round_index INTEGER DEFAULT 0', 'compress_failed_at INTEGER', 'thread_id TEXT', 'thread_model TEXT'):
                self._ensure_column(conn, 'conversations', column)
            self._ensure_column(conn, 'messages', 'thinking TEXT')
            # Pre-archive databases: preserve complete legacy pairs without deleting messages.
            conversations = conn.execute('SELECT id FROM conversations c WHERE NOT EXISTS (SELECT 1 FROM full_archive a WHERE a.conversation_id=c.id)').fetchall()
            for conversation in conversations:
                rows = conn.execute('SELECT role,content,created_at FROM messages WHERE conversation_id=? ORDER BY id', (conversation['id'],)).fetchall()
                pending = None
                index = 0
                for row in rows:
                    if row['role'] == 'user':
                        pending = row
                    elif row['role'] == 'assistant' and pending is not None:
                        self._archive_message(conn, conversation['id'], 'user', str(pending['content'] or ''), index, pending['created_at'] or int(time.time()))
                        self._archive_message(conn, conversation['id'], 'assistant', str(row['content'] or ''), index, row['created_at'] or int(time.time()))
                        index += 1
                        pending = None
                if index:
                    conn.execute('UPDATE conversations SET next_round_index=MAX(COALESCE(next_round_index,0),?) WHERE id=?', (index, conversation['id']))
        from app.memory_store import initialize
        initialize(self)

    def _archive_message(self, conn, conversation_id, role, content, round_index, created_at):
        conn.execute('INSERT OR IGNORE INTO full_archive(conversation_id,role,content,round_index,created_at) VALUES(?,?,?,?,?)',
                     (conversation_id, role, content, round_index, created_at))

    def _count_messages(self, conn, conversation_id):
        return conn.execute('SELECT COUNT(*) FROM messages WHERE conversation_id=?', (conversation_id,)).fetchone()[0]

    def new_conversation(self):
        conversation_id = str(uuid.uuid4())
        with self._get_conn() as conn:
            conn.execute('INSERT INTO conversations(id,title,created_at,next_round_index) VALUES(?,?,?,0)',
                         (conversation_id, 'New Chat', int(time.time())))
        return conversation_id

    def conversation_exists(self, conversation_id):
        with self._get_conn() as conn:
            return conn.execute('SELECT 1 FROM conversations WHERE id=?', (conversation_id,)).fetchone() is not None

    def delete_conversation(self, conversation_id):
        with self._get_conn() as conn:
            return conn.execute('DELETE FROM conversations WHERE id=?', (conversation_id,)).rowcount > 0

    def add_message(self, conversation_id, role, content, thinking=''):
        from app.memory_store import add_message
        return add_message(self, conversation_id, role, content, thinking)

    def persist_round(self, conversation_id, user_prompt, assistant_reply, assistant_thinking=''):
        from app.memory_store import persist_round
        return persist_round(self, conversation_id, user_prompt, assistant_reply, assistant_thinking)

    def get_conversation_thread_id(self, conversation_id):
        from app.memory_store import thread_binding
        return thread_binding(self, conversation_id).get('thread_id')

    def get_conversation_thread_model(self, conversation_id):
        from app.memory_store import thread_binding
        return thread_binding(self, conversation_id).get('thread_model')

    def set_conversation_thread_id(self, conversation_id, thread_id, model_name=None):
        with self._get_conn() as conn:
            conn.execute('UPDATE conversations SET thread_id=?,thread_model=?,thread_owner=NULL WHERE id=?',
                         (thread_id, model_name, conversation_id))

    def clear_conversation_thread(self, conversation_id):
        with self._get_conn() as conn:
            conn.execute('UPDATE conversations SET thread_id=NULL,thread_model=NULL,thread_owner=NULL WHERE id=?', (conversation_id,))

    def get_sliding_window(self, conn, conversation_id, limit_rounds=None):
        rows = conn.execute("SELECT * FROM sliding_window WHERE conversation_id=? AND compress_status!='compressed' ORDER BY round_number DESC LIMIT ?",
                            (conversation_id, limit_rounds or self.WINDOW_ROUNDS)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _fetch_recent_done_summaries(self, conn, conversation_id):
        rows = conn.execute("SELECT summary FROM compressed_summaries WHERE conversation_id=? AND compress_status='done' AND COALESCE(summary,'')!='' ORDER BY round_index DESC LIMIT ?",
                            (conversation_id, self.SUMMARY_INJECT_LIMIT)).fetchall()
        return [row['summary'] for row in reversed(rows)]

    def _has_failed_compression(self, conn, conversation_id):
        row = conn.execute('SELECT compress_failed_at FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        return bool(row and row['compress_failed_at'])

    def _search_recall_round_indices(self, conn, conversation_id, query):
        ignored = {'remember', 'earlier', 'before', 'recall', 'what', 'that', 'the', 'previous'}
        words = list(dict.fromkeys(word for word in re.findall(r'[\w-]{3,}', query.lower()) if word not in ignored))[:10]
        if not words:
            return []
        where = ' OR '.join('LOWER(content) LIKE ? ESCAPE "\\"' for _ in words)
        patterns = ['%' + word.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%' for word in words]
        rows = conn.execute(f'SELECT DISTINCT round_index FROM full_archive WHERE conversation_id=? AND ({where}) ORDER BY round_index DESC LIMIT ?',
                            (conversation_id, *patterns, self.RECALL_LIMIT)).fetchall()
        return sorted(row['round_index'] for row in rows)

    def _format_recalled_archive(self, conn, conversation_id, round_indices):
        if not round_indices:
            return ''
        placeholders = ','.join('?' for _ in round_indices)
        rows = conn.execute(f'SELECT role,content FROM full_archive WHERE conversation_id=? AND round_index IN ({placeholders}) ORDER BY round_index,id',
                            (conversation_id, *round_indices)).fetchall()
        return '\n'.join(f"{row['role']}: {row['content']}" for row in rows)

    def _build_config_block(self, model_name, *, gemini_mode=False):
        block = _config_block(model_name)
        block['value'].update(useWebSearch=True, useReadOnlyMode=True,
                              enableAgentIntegrations=False, enableAgentAutomations=False, enableCustomAgents=False)
        return block

    def _build_context_block(self, notion_client, *, gemini_mode=False):
        return _context_block({'user_id': notion_client.user_id, 'space_id': notion_client.space_id})

    def _build_dialog_block(self, role, content, notion_client, *, gemini_mode=False):
        return _assistant_block(content) if role == 'assistant' else _user_block(content, {'user_id': notion_client.user_id})

    def get_transcript_payload(self, notion_client, conversation_id, user_prompt, model_name, recall_query=None):
        from app.memory_store import build_payload
        transcript, degraded = build_payload(self, conversation_id, user_prompt, notion_client, model_name, recall_query or '')
        return {'transcript': transcript, 'memory_degraded': degraded}

    def get_transcript(self, notion_client, conversation_id, user_prompt, model_name):
        return self.get_transcript_payload(notion_client, conversation_id, user_prompt, model_name)['transcript']


def build_lite_transcript(user_prompt, model_name):
    return [_config_block(model_name), _user_block(user_prompt)]


def build_standard_transcript(messages, model_name, account):
    from app.proxy_contracts import build_transcript
    transcript = build_transcript(messages, model_name, account)
    transcript[0]['value']['useWebSearch'] = True
    return transcript


async def compress_round_if_needed(manager, conversation_id):
    from app.memory_store import compress_overflow
    await compress_overflow(manager, conversation_id)


async def compress_sliding_window_round(manager, conversation_id, round_number):
    from app.memory_store import compress_one
    await compress_one(manager, conversation_id, round_number)
