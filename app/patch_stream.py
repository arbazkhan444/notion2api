"""Apply typed NDJSON patches without classifying answer text by keywords."""
from __future__ import annotations
import copy
import json
from requests.exceptions import ChunkedEncodingError


def _parts(patch):
    raw = next((patch[key] for key in ('path', 'p', 'pointer', 'at') if key in patch), '')
    if isinstance(raw, (list, tuple)):
        return [str(part) for part in raw]
    return [part.replace('~1', '/').replace('~0', '~') for part in str(raw).strip('/').split('/') if part]


def _index(part):
    if not part.isdecimal() or (len(part) > 1 and part.startswith('0')):
        raise ValueError('Invalid array index')
    return int(part)


def _read(parent, part):
    return parent[_index(part)] if isinstance(parent, list) else parent[part]


def _apply(document, patch):
    parts = _parts(patch)
    if not parts or parts[0] != 's':
        return
    parent = document
    try:
        for part in parts[:-1]:
            parent = _read(parent, part)
        key = parts[-1]
        operation, value = patch.get('o'), copy.deepcopy(patch.get('v'))
        if operation not in ('a', 'p', 'x', 'd'):
            raise ValueError('Unknown patch operation')
        if isinstance(parent, list):
            index = len(parent) if key == '-' and operation == 'a' else _index(key)
            if operation == 'a':
                if not 0 <= index <= len(parent):
                    raise IndexError(index)
                parent.insert(index, value)
            elif operation == 'p':
                parent[index] = value
            elif operation == 'x':
                if not isinstance(parent[index], str) or not isinstance(value, str):
                    raise TypeError('Text append on non-text value')
                parent[index] += value
            else:
                del parent[index]
        elif isinstance(parent, dict):
            if operation in ('a', 'p'):
                parent[key] = value
            elif operation == 'x':
                if not isinstance(parent.get(key), str) or not isinstance(value, str):
                    raise TypeError('Text append on non-text value')
                parent[key] += value
            else:
                parent.pop(key, None)
        else:
            raise TypeError('Patch parent is not a container')
        if not isinstance(document.get('s'), list):
            raise TypeError('Transcript state must be an array')
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ChunkedEncodingError('Upstream patch referenced an unknown or invalid text path.') from exc


def _texts(step, inherited='content'):
    if not isinstance(step, dict):
        return []
    kind = str(step.get('type') or '').lower()
    if kind in ('thinking', 'reasoning', 'inference'):
        role = 'thinking'
    elif kind in ('text', 'agent-inference', 'markdown-chat', 'paragraph'):
        role = inherited
    else:
        return []
    value = step.get('value')
    if isinstance(step.get('content'), str):
        return [(role, step['content'])]
    if isinstance(value, str):
        return [(role, value)]
    result = []
    if isinstance(value, dict):
        result.extend(_texts(value, role))
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                result.extend(_texts(item, role))
            elif isinstance(item, str) and kind == 'markdown-chat':
                result.append((role, item))
    return result


def _snapshot(document, initial_refs, initial_ids):
    result = {'content': '', 'thinking': ''}
    for step in document.get('s', []):
        # Object identity keeps inserted-at-zero responses separate from input
        # history. Stable IDs also exclude input records in a full replacement.
        if id(step) in initial_refs or (isinstance(step, dict) and step.get('id') in initial_ids):
            continue
        for role, text in _texts(step):
            result[role] += text
    return result


def parse(response, initial_transcript=None):
    from app.stream_parser import (_clean_extracted_text, _extract_final_content_from_record_map,
                                   _extract_markdown_chat_text, _extract_search_data_from_patch)
    initial = copy.deepcopy(initial_transcript or [])
    document = {'s': initial}
    initial_refs = {id(step) for step in initial}
    initial_ids = {step['id'] for step in initial if isinstance(step, dict) and isinstance(step.get('id'), str)}
    previous = {'content': '', 'thinking': ''}
    fallback = None
    markdown_final = None
    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            if isinstance(line, bytes):
                line = line.decode('utf-8', errors='strict')
            event = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise ChunkedEncodingError('Malformed upstream NDJSON.') from exc
        if not isinstance(event, dict):
            raise ChunkedEncodingError('Upstream event must be an object.')
        kind = str(event.get('type') or '').lower()
        if kind in ('error', 'failed') or event.get('error'):
            raise ChunkedEncodingError('Upstream reported a failed operation.')
        if kind == 'record-map':
            candidate = _extract_final_content_from_record_map(event)
            if candidate:
                fallback = candidate['text']
            continue
        if kind == 'markdown-chat':
            markdown_final = _extract_markdown_chat_text(event.get('value'))
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
            if any(token in nested_type for token in ('tool', 'search', 'citation')):
                metadata = _extract_search_data_from_patch(patch)
                if metadata:
                    yield {'type': 'search', 'data': metadata}
            _apply(document, patch)
            current = _snapshot(document, initial_refs, initial_ids)
            for role, text in current.items():
                old = previous[role]
                if text != old:
                    if text.startswith(old):
                        yield {'type': role, 'text': text[len(old):]}
                    else:
                        yield {'type': role + '_replace', 'text': text}
            previous = current
    final = _snapshot(document, initial_refs, initial_ids)
    # A direct markdown-chat snapshot refers to this response. An uncorrelated
    # record-map is only a fallback; it must not overwrite reconstructed output.
    body = markdown_final if markdown_final is not None else final['content'] or fallback or ''
    yield {'type': 'final_thinking', 'text': final['thinking']}
    yield {'type': 'final_content', 'text': _clean_extracted_text(body),
           'source_type': 'markdown-chat' if markdown_final is not None else 'reconstructed-patches' if final['content'] else 'record-map'}
