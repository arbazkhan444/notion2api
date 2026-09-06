"""NDJSON entrypoint and protocol-typed metadata helpers."""
import json
import re


def _strip_primary_attr_fragments(text, in_primary_attr):
    return text


def _strip_lang_tags(text, in_tag):
    return text


def _clean_notion_markup(text):
    # Only a complete outer protocol wrapper, never HTML/code inside an answer.
    match = re.fullmatch(r'\s*<lang\s+primary=[\x22\x27][A-Za-z-]+[\x22\x27]>(.*)</lang>\s*', text, re.DOTALL)
    return match.group(1) if match else text


def _clean_extracted_text(text):
    return _clean_notion_markup(text or '')


def _looks_like_search_json_fragment(text):
    return False


def _extract_markdown_chat_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ''.join(_extract_markdown_chat_text(item) for item in value)
    if isinstance(value, dict):
        if value.get('type') in ('thinking', 'reasoning', 'tool'):
            return ''
        for key in ('content', 'text', 'value'):
            if key in value:
                return _extract_markdown_chat_text(value[key])
    return ''


def _extract_final_content_from_record_map(data):
    record_map = data.get('recordMap', {})
    records = record_map.get('thread_message', {}) if isinstance(record_map, dict) else {}
    if not isinstance(records, dict):
        return None
    candidates = []
    for message_id, record in records.items():
        if not isinstance(record, dict):
            continue
        outer = record.get('value') or {}
        inner = outer.get('value') if isinstance(outer, dict) else None
        step = inner.get('step') if isinstance(inner, dict) else None
        if not isinstance(step, dict) or step.get('type') not in ('text', 'agent-inference', 'markdown-chat'):
            continue
        text = _clean_extracted_text(_extract_markdown_chat_text(step.get('value')))
        if not text:
            continue
        def number(key):
            try:
                return int(outer.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        priority = {'markdown-chat': 3, 'text': 2, 'agent-inference': 1}[step['type']]
        candidates.append(((number('last_edited_time'), number('created_time'), priority),
                           {'text': text, 'source_type': step['type'], 'source_message_id': str(message_id)}))
    return max(candidates, key=lambda candidate: candidate[0])[1] if candidates else None


def _extract_search_data_from_patch(patch):
    queries, sources = [], []
    def visit(value):
        if isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, dict):
            for key in ('queries', 'questions'):
                if isinstance(value.get(key), list):
                    queries.extend(q for q in value[key] if isinstance(q, str))
            if isinstance(value.get('query'), str):
                queries.append(value['query'])
            for key in ('sources', 'citations', 'results'):
                items = value.get(key)
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            url = item.get('url') or item.get('href') or item.get('link') or ''
                            if isinstance(url, str) and url.startswith(('https://', 'http://')):
                                source = {'url': url, 'title': str(item.get('title') or item.get('name') or url),
                                          'snippet': str(item.get('snippet') or item.get('summary') or '')}
                                if source not in sources:
                                    sources.append(source)
            for child in value.values():
                if isinstance(child, (list, dict)):
                    visit(child)
    visit(patch.get('v'))
    result = {}
    if queries:
        result['queries'] = list(dict.fromkeys(queries))
    if sources:
        result['sources'] = sources
    return result


def parse_stream(response, initial_transcript=None):
    from app.patch_stream import parse
    yield from parse(response, initial_transcript)
