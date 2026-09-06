"""Structural NDJSON patch application.

Text is never classified from words such as primary, questions or sources.
Replacement patches update the document; they are not append deltas. Unknown
text paths fail closed instead of being attached to an arbitrary segment.
"""
from __future__ import annotations

import copy
import json

from requests.exceptions import ChunkedEncodingError


def _parts(patch):
    raw = next((patch[k] for k in ('path', 'p', 'pointer', 'at') if k in patch), '')
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw]
    return [p.replace('~1', '/').replace('~0', '~') for p in str(raw).strip('/').split('/') if p]


def _read(parent, part):
    return parent[int(part)] if isinstance(parent, list) else parent[part]


def _apply(document, patch):
    parts = _parts(patch)
    if not parts or parts[0] != 's':
        return
    parent = document
    try:
        for part in parts[:-1]:
            parent = _read(parent, part)
        key = parts[-1]
        op, value = patch.get('o'), copy.deepcopy(patch.get('v'))
        if isinstance(parent, list):
            index = len(parent) if key == '-' else int(key)
            if op == 'a':
                if not 0 <= index <= len(parent):
                    raise IndexError(index)
                parent.insert(index, value)
            elif op == 'p':
                parent[index] = value
            elif op == 'x':
                if not isinstance(parent[index], str) or not isinstance(value, str):
                    raise TypeError('Text append on non-text value')
                parent[index] += value
            elif op == 'd':
                del parent[index]
        elif isinstance(parent, dict):
            if op in ('a', 'p'):
                parent[key] = value
            elif op == 'x':
                if not isinstance(parent.get(key), str) or not isinstance(value, str):
                    raise TypeError('Text append on non-text value')
                parent[key] += value
            elif op == 'd':
                parent.pop(key, None)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ChunkedEncodingError('Upstream patch referenced an unknown or invalid text path.') from exc


def _texts(step, path):
    if not isinstance(step, dict):
        return []
    kind = str(step.get('type') or '').lower()
    if kind in ('config', 'context', 'user', 'assistant', 'title') or any(k in kind for k in ('tool', 'search', 'citation')):
        return []
    role = 'thinking' if kind in ('thinking', 'reasoning', 'inference') else 'content'
    value = step.get('value')
    if isinstance(step.get('content'), str):
        return [(path + ('content',), role, step['content'])]
    if isinstance(value, str):
        return [(path + ('value',), role, value)]
    result = []
    if isinstance(value, list):
        for i, item in enumerate(value):
            if isinstance(item, dict):
                result.extend(_texts(item, path + ('value', str(i))))
            elif isinstance(item, str) and kind == 'markdown-chat':
                result.append((path + ('value', str(i)), 'content', item))
    return result


def _snapshot(document, initial_count):
    result = []
    for index, step in enumerate(document.get('s', [])):
        if index >= initial_count:
            result.extend(_texts(step, ('s', str(index))))
    return result


def parse(response, initial_transcript=None):
    from app.stream_parser import (_clean_extracted_text, _extract_final_content_from_record_map,
                                   _extract_markdown_chat_text, _extract_search_data_from_patch)
    initial = copy.deepcopy(initial_transcript or [])
    document = {'s': initial}
    initial_count = len(initial)
    previous = {}
    final = None
    authoritative = False
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode('utf-8', errors='strict')
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ChunkedEncodingError('Malformed upstream NDJSON.') from exc
        if not isinstance(event, dict):
            raise ChunkedEncodingError('Upstream event must be an object.')
        kind = str(event.get('type') or '').lower()
        if kind in ('error', 'failed') or event.get('error'):
            raise ChunkedEncodingError('Upstream reported a failed operation.')
        if kind == 'record-map':
            # Prefer the reconstructed current response over unrelated history.
            candidate = _extract_final_content_from_record_map(event)
            if candidate:
                final = candidate['text']
                authoritative = True
            continue
        if kind == 'markdown-chat':
            final = _extract_markdown_chat_text(event.get('value'))
            authoritative = True
            continue
        if kind != 'patch':
            continue
        patches = event.get('v')
        if not isinstance(patches, list):
            raise ChunkedEncodingError('Patch event must contain an array.')
        for patch in patches:
            if not isinstance(patch, dict):
                raise ChunkedEncodingError('Patch must be an object.')
            parts = _parts(patch)
            if not parts or parts[0] != 's':
                continue
            value = patch.get('v')
            nested_type = str(value.get('type') or '').lower() if isinstance(value, dict) else ''
            # Metadata is recognized by its protocol type, not the JSON inside answer text.
            if any(t in nested_type for t in ('tool', 'search', 'citation')):
                metadata = _extract_search_data_from_patch(patch)
                if metadata:
                    yield {'type': 'search', 'data': metadata}
            _apply(document, patch)
            current = _snapshot(document, initial_count)
            for path, role, text in current:
                old = previous.get(path, '')
                if text.startswith(old) and text != old:
                    yield {'type': role, 'text': text[len(old):]}
            previous = {path: text for path, _, text in current}
    snapshot = _snapshot(document, initial_count)
    body = ''.join(text for _, role, text in snapshot if role == 'content')
    if body or not authoritative:
        final = body
    if final is not None:
        yield {'type': 'final_content', 'text': _clean_extracted_text(final), 'source_type': 'reconstructed-patches' if body else 'record-map'}
