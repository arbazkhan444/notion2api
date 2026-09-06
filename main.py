"""Interactive client: store only finalized, successful turns."""
import asyncio
import sys


def main():
    try:
        from app.config import get_default_account
        from app.conversation import ConversationManager
        from app.memory_store import compress_overflow
        from app.notion_client import NotionOpusAPI
        client = NotionOpusAPI(get_default_account())
        manager = ConversationManager()
    except (ValueError, OSError) as exc:
        print(f'[Configuration error] {type(exc).__name__}. Check your local configuration.')
        return 1
    print("Notion Opus terminal — 'exit' to quit, 'new' for a new conversation.")
    current_conv = manager.new_conversation()
    while True:
        try:
            prompt = input('\n[You]: ').strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not prompt:
            continue
        if prompt.lower() == 'exit':
            break
        if prompt.lower() == 'new':
            current_conv = manager.new_conversation()
            print('New conversation started.')
            continue
        stream = None
        saved = False
        try:
            transcript = manager.get_transcript(client, current_conv, prompt, 'claude-opus4.6')
            stream = client.stream_response(transcript)
            answer, thinking = '', ''
            for item in stream:
                if isinstance(item, str):
                    answer += item
                elif isinstance(item, dict):
                    kind, value = item.get('type'), str(item.get('text') or '')
                    if kind == 'content':
                        answer += value
                    elif kind in ('content_replace', 'final_content'):
                        answer = value
                    elif kind == 'thinking':
                        thinking += value
                    elif kind in ('thinking_replace', 'final_thinking'):
                        thinking = value
                    elif kind == 'error':
                        raise RuntimeError('Upstream reported an error.')
            if not answer and not thinking:
                raise RuntimeError('No response received.')
            manager.persist_round(current_conv, prompt, answer, thinking)
            saved = True
            print('\n[AI]: ' + (answer or '[No visible answer; reasoning only.]'))
        except KeyboardInterrupt:
            print('\n[Interrupted] ' + ('The completed turn was saved.' if saved else 'This turn was not saved.'))
        except Exception as exc:
            print(f'\n[Error] {type(exc).__name__}. ' + ('The completed turn was saved.' if saved else 'This turn was not saved.'))
        finally:
            if stream is not None:
                stream.close()
        if saved:
            try:
                asyncio.run(compress_overflow(manager, current_conv))
            except (Exception, KeyboardInterrupt):
                print('[Memory] Compression interrupted; the raw turn remains stored.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
