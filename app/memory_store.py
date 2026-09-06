"""Atomic turn storage, idempotent imports, and leased compression.

The archive is retained. Migration fills missing window rows without deleting
history. Existing conflicting archive records require manual reconciliation.
"""
from __future__ import annotations

import time
from collections import defaultdict

from app.model_registry import is_gemini_model


def initialize(manager):
    with manager._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        manager._ensure_column(conn, 'conversations', 'thread_owner TEXT')
        manager._ensure_column(conn, 'sliding_window', 'compression_started_at REAL')
        # Retain duplicate historical summaries for audit, but inject one per turn.
        conn.execute("""UPDATE compressed_summaries SET compress_status='superseded'
            WHERE compress_status='done' AND id NOT IN (
                SELECT MAX(id) FROM compressed_summaries WHERE compress_status='done'
                GROUP BY conversation_id, round_index)""")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_summary_done_unique
            ON compressed_summaries(conversation_id, round_index)
            WHERE compress_status='done'""")


def _next_round(conn, conversation_id):
    row = conn.execute('SELECT next_round_index FROM conversations WHERE id=?', (conversation_id,)).fetchone()
    if row is None:
        raise ValueError('Conversation not found.')
    return int(row['next_round_index'] or 0)


def _write_pair(manager, conn, conversation_id, user, assistant, thinking=''):
    index = _next_round(conn, conversation_id)
    now = int(time.time())
    for role, text, thought in [('user', user, ''), ('assistant', assistant, thinking)]:
        conn.execute('INSERT INTO messages(conversation_id,role,content,thinking,created_at) VALUES(?,?,?,?,?)',
                     (conversation_id, role, text, thought, now))
        manager._archive_message(conn, conversation_id, role, text, index, now)
    # A conflict is a consistency error, never permission to overwrite a turn.
    conn.execute("""INSERT INTO sliding_window(conversation_id,round_number,user_content,
        assistant_content,assistant_thinking,compress_status,created_at) VALUES(?,?,?,?,?,'active',?)""",
        (conversation_id, index, user, assistant, thinking, now))
    conn.execute('UPDATE conversations SET next_round_index=? WHERE id=?', (index + 1, conversation_id))
    return index


def persist_round(manager, conversation_id, user_prompt, assistant_reply, assistant_thinking=''):
    with manager._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return _write_pair(manager, conn, conversation_id, user_prompt, assistant_reply, assistant_thinking)


def add_message(manager, conversation_id, role, content, thinking=''):
    """Compatibility for the CLI: complete a window turn on assistant insertion."""
    if role not in ('user', 'assistant', 'system'):
        raise ValueError('Invalid message role.')
    with manager._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        index = _next_round(conn, conversation_id)
        previous = conn.execute('SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 1',
                                (conversation_id,)).fetchone()
        now = int(time.time())
        conn.execute('INSERT INTO messages(conversation_id,role,content,thinking,created_at) VALUES(?,?,?,?,?)',
                     (conversation_id, role, content, thinking, now))
        manager._archive_message(conn, conversation_id, role, content, index, now)
        if role == 'assistant' and previous and previous['role'] == 'user':
            conn.execute("""INSERT INTO sliding_window(conversation_id,round_number,user_content,
                assistant_content,assistant_thinking,compress_status,created_at) VALUES(?,?,?,?,?,'active',?)""",
                (conversation_id, index, previous['content'], content, thinking, now))
            conn.execute('UPDATE conversations SET next_round_index=? WHERE id=?', (index + 1, conversation_id))


def ensure_window(manager, conversation_id):
    with manager._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute("SELECT round_index,role,content,created_at FROM full_archive WHERE conversation_id=? AND role IN ('user','assistant') ORDER BY id", (conversation_id,)).fetchall()
        grouped = defaultdict(dict)
        for row in rows:
            grouped[row['round_index']][row['role']] = row
        for index, pair in grouped.items():
            if 'user' not in pair or 'assistant' not in pair:
                continue
            done = conn.execute("SELECT 1 FROM compressed_summaries WHERE conversation_id=? AND round_index=? AND compress_status='done'", (conversation_id, index)).fetchone()
            conn.execute("""INSERT OR IGNORE INTO sliding_window(conversation_id,round_number,user_content,
                assistant_content,assistant_thinking,compress_status,created_at) VALUES(?,?,?,?,'',?,?)""",
                (conversation_id, index, pair['user']['content'], pair['assistant']['content'],
                 'compressed' if done else 'active', pair['assistant']['created_at']))
        max_row = conn.execute('SELECT MAX(round_number) AS n FROM sliding_window WHERE conversation_id=?', (conversation_id,)).fetchone()
        if max_row['n'] is not None:
            conn.execute('UPDATE conversations SET next_round_index=MAX(COALESCE(next_round_index,0),?) WHERE id=?',
                         (int(max_row['n']) + 1, conversation_id))


def import_history(manager, conversation_id, history):
    """Accept a full-history prefix or an overlapping client window, not a fork."""
    ensure_window(manager, conversation_id)
    entries = [(role, str(content or ''), str(thinking or '')) for role, content, thinking in history
               if role in ('user', 'assistant')]
    if len(entries) % 2 or any(role != ('user' if i % 2 == 0 else 'assistant') for i, (role, _, _) in enumerate(entries)):
        raise ValueError('History must contain complete ordered user/assistant turns.')
    incoming = [(role, content) for role, content, _ in entries]
    with manager._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        _next_round(conn, conversation_id)
        rows = conn.execute('SELECT user_content,assistant_content FROM sliding_window WHERE conversation_id=? ORDER BY round_number', (conversation_id,)).fetchall()
        stored = [item for row in rows for item in [('user', row['user_content']), ('assistant', row['assistant_content'])]]
        if incoming == stored[:len(incoming)]:
            return
        if stored == incoming[:len(stored)]:
            overlap = len(stored)
        else:
            overlap = next((n for n in range(min(len(stored), len(incoming)), 1, -2)
                            if stored[-n:] == incoming[:n]), 0)
            if stored and incoming and overlap == 0:
                raise ValueError('History conflicts with this conversation; use a new conversation_id.')
        for offset in range(overlap, len(entries), 2):
            _write_pair(manager, conn, conversation_id, entries[offset][1], entries[offset + 1][1], entries[offset + 1][2])


def thread_binding(manager, conversation_id):
    with manager._get_conn() as conn:
        row = conn.execute('SELECT thread_id,thread_model,thread_owner FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        return dict(row) if row else {}


def bind_thread(manager, conversation_id, thread_id, model, owner):
    with manager._get_conn() as conn:
        conn.execute('UPDATE conversations SET thread_id=?,thread_model=?,thread_owner=? WHERE id=?',
                     (thread_id, model, owner, conversation_id))


def build_payload(manager, conversation_id, user_prompt, client, model_name, recall_query=''):
    ensure_window(manager, conversation_id)
    with manager._get_conn() as conn:
        rows = conn.execute("""SELECT * FROM sliding_window WHERE conversation_id=?
            AND compress_status!='compressed' ORDER BY round_number DESC LIMIT ?""",
            (conversation_id, manager.WINDOW_ROUNDS)).fetchall()
        active = conn.execute("SELECT COUNT(*) FROM sliding_window WHERE conversation_id=? AND compress_status!='compressed'", (conversation_id,)).fetchone()[0]
        failed = conn.execute('SELECT compress_failed_at FROM conversations WHERE id=?', (conversation_id,)).fetchone()
        summaries = manager._fetch_recent_done_summaries(conn, conversation_id)
        recalled = ''
        if recall_query:
            indices = manager._search_recall_round_indices(conn, conversation_id, recall_query)
            recalled = manager._format_recalled_archive(conn, conversation_id, indices)
    gemini = is_gemini_model(model_name)
    transcript = [manager._build_config_block(model_name, gemini_mode=gemini),
                  manager._build_context_block(client, gemini_mode=gemini)]
    memory = '\n\n'.join(summaries + ([recalled] if recalled else []))
    if memory:
        transcript.append(manager._build_dialog_block('user', '[Earlier conversation memory]\n' + memory, client, gemini_mode=gemini))
    for row in reversed(rows):
        for role, key in [('user', 'user_content'), ('assistant', 'assistant_content')]:
            transcript.append(manager._build_dialog_block(role, row[key], client, gemini_mode=gemini))
    transcript.append(manager._build_dialog_block('user', user_prompt, client, gemini_mode=gemini))
    return transcript, bool(active > manager.WINDOW_ROUNDS or (failed and failed['compress_failed_at']))


async def compress_one(manager, conversation_id, round_number):
    from app.summarizer import is_summarizer_configured, summarize_turn
    if not is_summarizer_configured():
        return
    started = time.time()
    with manager._get_conn() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM sliding_window WHERE conversation_id=? AND round_number=?', (conversation_id, round_number)).fetchone()
        if not row or row['compress_status'] == 'compressed':
            return
        if row['compress_status'] == 'compressing' and (row['compression_started_at'] or 0) > started - 120:
            return
        conn.execute("UPDATE sliding_window SET compress_status='compressing',compression_started_at=? WHERE conversation_id=? AND round_number=?",
                     (started, conversation_id, round_number))
        summaries = manager._fetch_recent_done_summaries(conn, conversation_id)
    committed = False
    try:
        summary = (await summarize_turn(summaries, row['user_content'], row['assistant_content'])).strip()
        if not summary:
            raise ValueError('Empty summary.')
        with manager._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            owned = conn.execute("SELECT 1 FROM sliding_window WHERE conversation_id=? AND round_number=? AND compress_status='compressing' AND compression_started_at=?", (conversation_id, round_number, started)).fetchone()
            if not owned:
                return
            conn.execute("""INSERT OR IGNORE INTO compressed_summaries(conversation_id,round_index,
                user_content,assistant_content,summary,compress_status,created_at) VALUES(?,?,?,?,?,'done',?)""",
                (conversation_id, round_number, row['user_content'], row['assistant_content'], summary, int(time.time())))
            conn.execute("UPDATE sliding_window SET compress_status='compressed',compression_started_at=NULL WHERE conversation_id=? AND round_number=?", (conversation_id, round_number))
            conn.execute('UPDATE conversations SET compress_failed_at=NULL WHERE id=?', (conversation_id,))
        committed = True
    finally:
        # Includes cancellation; a crashed process leaves a lease recoverable after 120s.
        if not committed:
            with manager._get_conn() as conn:
                changed = conn.execute("UPDATE sliding_window SET compress_status='active',compression_started_at=NULL WHERE conversation_id=? AND round_number=? AND compression_started_at=?",
                                       (conversation_id, round_number, started)).rowcount
                if changed:
                    conn.execute('UPDATE conversations SET compress_failed_at=? WHERE id=?', (int(time.time()), conversation_id))


async def compress_overflow(manager, conversation_id):
    with manager._get_conn() as conn:
        rows = conn.execute("SELECT round_number FROM sliding_window WHERE conversation_id=? AND compress_status!='compressed' ORDER BY round_number DESC", (conversation_id,)).fetchall()
    for row in reversed(rows[manager.WINDOW_ROUNDS:]):
        try:
            await compress_one(manager, conversation_id, row['round_number'])
        except Exception:
            # Failure is recorded; raw context remains active and is never silently marked done.
            return
