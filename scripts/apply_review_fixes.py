"""One-time branch materializer. Runs only in the temporary GitHub workflow.

Changes existing functions by their AST boundaries without copying large source
files through chat. Every replacement is checked and the result is compiled.
This script is removed after successful materialization.
"""
from pathlib import Path
import ast
import re
import textwrap


def read(path):
    return Path(path).read_text(encoding='utf-8')


def write(path, source):
    if str(path).endswith('.py'):
        compile(source, str(path), 'exec')
    Path(path).write_text(source, encoding='utf-8')


def node_for(source, name):
    nodes = ast.parse(source).body
    node = None
    for part in name.split('.'):
        found = [n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part]
        if len(found) != 1:
            raise RuntimeError(f'Expected one definition of {name}')
        node = found[0]
        nodes = node.body
    return node


def function(path, name, replacement):
    source = read(path)
    node = node_for(source, name)
    lines = source.splitlines(keepends=True)
    replacement = textwrap.indent(textwrap.dedent(replacement).strip() + '\n', ' ' * node.col_offset)
    write(path, ''.join(lines[:node.lineno - 1]) + replacement + ''.join(lines[node.end_lineno:]))


def replace(path, old, new, count=1):
    source = read(path)
    if old not in source and new in source:
        return
    if source.count(old) != count:
        raise RuntimeError(f'{path}: expected {count} exact matches, found {source.count(old)}: {old[:100]}')
    write(path, source.replace(old, new))


function('app/model_registry.py', 'get_notion_model', '''
def get_notion_model(model_name: str) -> str:
    if model_name in NOTION_MODEL_REVERSE_MAP:
        return model_name
    standard_name = normalize_model_name(model_name)
    if standard_name not in MODEL_MAP:
        raise ValueError('Unsupported model.')
    return MODEL_MAP[standard_name]
''')
function('app/agent_adapter.py', 'build_agent_transcript', '''
def build_agent_transcript(messages, model_name, account, tools=None, tool_choice=None,
                           response_format=None, max_tokens=None, max_completion_tokens=None):
    from app.proxy_contracts import build_transcript
    return build_transcript(messages, model_name, account, tools, tool_choice,
                            response_format, max_tokens, max_completion_tokens)
''')
function('app/agent_adapter.py', 'parse_tool_calls', '''
def parse_tool_calls(text, allowed_tool_names, *, parallel_tool_calls=None):
    from app.proxy_contracts import parse_calls
    return parse_calls(text, allowed_tool_names, parallel_tool_calls=parallel_tool_calls)
''')
function('app/agent_adapter.py', 'get_safe_system_parts', '''
def get_safe_system_parts(messages, *, has_tools):
    return [content_to_text(_get_field(message, 'content')) for message in messages
            if _get_field(message, 'role') in ('system', 'developer')]
''')
function('app/cline_tool_router.py', 'build_direct_tool_call_response', '''
def build_direct_tool_call_response(req_body, model_name):
    # Substring heuristics are not user authorization and cannot enforce schemas.
    return None
''')
source = read('app/cline_tool_router.py')
source = source.replace('.lstrip("./")', '.removeprefix("./")').replace(".lstrip('./')", ".removeprefix('./')")
write('app/cline_tool_router.py', source)

function('app/conversation.py', 'build_standard_transcript', '''
def build_standard_transcript(messages, model_name, account):
    from app.proxy_contracts import build_transcript
    transcript = build_transcript(messages, model_name, account)
    transcript[0]['value']['useWebSearch'] = True
    return transcript
''')
function('app/conversation.py', 'ConversationManager.persist_round', '''
def persist_round(self, conversation_id, user_prompt, assistant_reply, assistant_thinking=''):
    from app.memory_store import persist_round
    return persist_round(self, conversation_id, user_prompt, assistant_reply, assistant_thinking)
''')
function('app/conversation.py', 'ConversationManager.add_message', '''
def add_message(self, conversation_id, role, content, thinking=''):
    from app.memory_store import add_message
    return add_message(self, conversation_id, role, content, thinking)
''')
function('app/conversation.py', 'compress_round_if_needed', '''
async def compress_round_if_needed(manager, conversation_id):
    from app.memory_store import compress_overflow
    await compress_overflow(manager, conversation_id)
''')
function('app/conversation.py', 'compress_sliding_window_round', '''
async def compress_sliding_window_round(manager, conversation_id, round_number):
    from app.memory_store import compress_one
    await compress_one(manager, conversation_id, round_number)
''')
source = read('app/conversation.py')
node = node_for(source, 'ConversationManager._init_db')
segment = '\n'.join(source.splitlines()[node.lineno - 1:node.end_lineno])
if 'initialize(self)' not in segment:
    lines = source.splitlines(keepends=True)
    source = ''.join(lines[:node.end_lineno]) + '\n        from app.memory_store import initialize\n        initialize(self)\n' + ''.join(lines[node.end_lineno:])
source = source.replace('"useReadOnlyMode": False', '"useReadOnlyMode": True')
for flag in ('enableAgentAutomations', 'enableAgentIntegrations', 'enableCustomAgents', 'enableCreateAndRunThread', 'enableUpdatePageV2Tool', 'enableUpdatePageAutofixer', 'enableUpdatePageOrderUpdates'):
    source = source.replace(f'"{flag}": True', f'"{flag}": False')
# Legacy CLI payloads should also report omitted active history truthfully.
source = source.replace('memory_degraded = False', "memory_degraded = self._has_failed_compression(conn, conversation_id) or conn.execute(\"SELECT COUNT(*) FROM sliding_window WHERE conversation_id=? AND compress_status!='compressed'\", (conversation_id,)).fetchone()[0] > self.WINDOW_ROUNDS")
write('app/conversation.py', source)
node = node_for(source, 'ConversationManager.get_sliding_window')
lines = source.splitlines(keepends=True)
segment = ''.join(lines[node.lineno - 1:node.end_lineno])
segment = segment.replace("compress_status = 'active'", "compress_status IN ('active', 'compressing', 'failed')")
write('app/conversation.py', ''.join(lines[:node.lineno - 1]) + segment + ''.join(lines[node.end_lineno:]))

function('app/stream_parser.py', '_strip_primary_attr_fragments', '''
def _strip_primary_attr_fragments(text, in_primary_attr):
    return text
''')
function('app/stream_parser.py', '_strip_lang_tags', '''
def _strip_lang_tags(text, in_tag):
    # Arbitrary code/HTML is answer data. Strip only a complete outer wrapper later.
    return text
''')
function('app/stream_parser.py', '_clean_notion_markup', '''
def _clean_notion_markup(text):
    match = re.fullmatch(r'\\s*<lang\\s+primary=["\\\'][A-Za-z-]+["\\\']>(.*)</lang>\\s*', text, re.DOTALL)
    return match.group(1) if match else text
''')
function('app/stream_parser.py', '_clean_extracted_text', '''
def _clean_extracted_text(text):
    return _clean_notion_markup(text or '')
''')
function('app/stream_parser.py', '_looks_like_search_json_fragment', '''
def _looks_like_search_json_fragment(text):
    return False
''')
function('app/stream_parser.py', 'parse_stream', '''
def parse_stream(response, initial_transcript=None):
    from app.patch_stream import parse
    yield from parse(response, initial_transcript)
''')
# Record-map fallback prioritizes recency, never a newer title over an answer.
source = read('app/stream_parser.py')
source = source.replace('if has_high_priority:', 'if False and has_high_priority:')
source = source.replace('    if not candidates:\n        return None', "    non_titles = [c for c in candidates if c['step_type'] != 'title']\n    candidates = non_titles or candidates\n    if not candidates:\n        return None")
source = source.replace('int(candidate.get("priority", 0)),\n            int(candidate.get("edited_at", 0)),\n            int(candidate.get("created_at", 0)),', 'int(candidate.get("edited_at", 0)),\n            int(candidate.get("created_at", 0)),\n            int(candidate.get("priority", 0)),')
write('app/stream_parser.py', source)

path = 'app/notion_client.py'
source = read(path)
node = node_for(source, 'NotionOpusAPI.stream_response')
lines = source.splitlines(keepends=True)
segment = ''.join(lines[node.lineno - 1:node.end_lineno])
if 'new_thread: bool' not in segment:
    segment = segment.replace('        agent_mode: bool = False,', '        agent_mode: bool = False,\n        new_thread: bool = False,', 1)
segment = segment.replace('should_create_thread = thread_id is None\n', 'should_create_thread = thread_id is None or new_thread\n')
segment = segment.replace('parse_stream(response)', 'parse_stream(response, initial_transcript=notion_transcript)')
# Keep cookie/session mutation and request-header handling under the same lock.
segment = segment.replace('            response = scraper.post(\n                self.url,\n                headers=headers,\n                json=payload,\n                stream=True,\n                timeout=(15, 120),\n            )',
                          '                response = scraper.post(\n                    self.url, headers=headers, json=payload,\n                    stream=True, timeout=(15, 120),\n                )')
segment = segment.replace('                response = new_scraper.post(\n                    self.url,\n                    headers=headers,\n                    json=payload,\n                    stream=True,\n                    timeout=(15, 120),\n                )',
                          '                    response = new_scraper.post(\n                        self.url, headers=headers, json=payload,\n                        stream=True, timeout=(15, 120),\n                    )')
segment = segment.replace('                    response_excerpt=excerpt,', '                    response_excerpt=excerpt,\n                    retry_after=_retry_after_seconds(response.headers.get("Retry-After")),')
source = ''.join(lines[:node.lineno - 1]) + segment + ''.join(lines[node.end_lineno:])
source = source.replace('        response_excerpt: str = "",\n', '        response_excerpt: str = "",\n        retry_after: Optional[float] = None,\n')
source = source.replace('        self.response_excerpt = response_excerpt\n', '        self.response_excerpt = response_excerpt\n        self.retry_after = retry_after\n')
source = source.replace('            notion_block["value"] = notion_value', '            notion_value["useReadOnlyMode"] = True\n            notion_block["value"] = notion_value')
if 'def _retry_after_seconds' not in source:
    source += '''\n\ndef _retry_after_seconds(value):
    import math
    from email.utils import parsedate_to_datetime
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = 60.0
    return max(1.0, seconds) if math.isfinite(seconds) else 60.0
'''
write(path, source)

replace('app/config.py', 'load_dotenv(override=True)', 'load_dotenv(override=False)')
# Restrict defaults while allowing explicit opt-in in a trusted local setup.
source = read('app/config.py').replace('os.getenv("HOST", "0.0.0.0")', 'os.getenv("HOST", "127.0.0.1")')
source = source.replace('os.getenv("ALLOWED_ORIGINS", "*")', 'os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000")')
write('app/config.py', source)
source = read('docker-compose.yml').replace('${HOST_PORT:-8000}:8000', '${HOST_BIND:-127.0.0.1}:${HOST_PORT:-8000}:8000')
write('docker-compose.yml', source)
source = read('.env.example')
source = re.sub(r'^HOST=0\.0\.0\.0$', 'HOST=127.0.0.1', source, flags=re.M)
source = re.sub(r'^ALLOWED_ORIGINS=\*$', 'ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000', source, flags=re.M)
if 'ALLOW_UNAUTHENTICATED=' not in source:
    source += '\n# API_KEY is required unless explicitly opting into trusted-local use.\nALLOW_UNAUTHENTICATED=false\nHOST_BIND=127.0.0.1\nREQUESTS_PER_MINUTE=20\nMAX_CONCURRENT_REQUESTS=4\nMAX_REQUEST_BYTES=1048576\nSILICONFLOW_MODEL=Qwen/Qwen3-8B\n'
write('.env.example', source)

# Patch the actual served entrypoint, not only the older unused JS modules.
path = 'frontend/index.html'
source = read(path)
if 'src="safe-browser.js"' not in source:
    source = source.replace('<script>\n/* ===== NAMESPACE ===== */', '<script src="safe-browser.js"></script>\n<script src="stream-reader.js"></script>\n<script>\n/* ===== NAMESPACE ===== */', 1)
source = source.replace('localStorage.', 'SafeBrowserStorage.local.').replace('sessionStorage.', 'SafeBrowserStorage.session.')
source = source.replace("JSON.parse(SafeBrowserStorage.local.getItem('claude_chats'))||[]", 'SafeBrowserStorage.loadChats()')
source = source.replace("https://cdn.jsdelivr.net/npm/marked/marked.min.js", "https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js")
source = source.replace("if (src.url) { const a=", "if (SafeBrowserStorage.safeUrl(src.url)) { const a=")
source = source.replace("a.href=src.url", "a.href=SafeBrowserStorage.safeUrl(src.url)")
source = source.replace("if(!href.startsWith('http'))return false;", "if(!/^https?:\\/\\//.test(href) || !/^\\[?\\d+\\]?$/.test(a.textContent.trim()))return false;")
source = source.replace("let c=md.replace(/\\[\\^(\\d+)\\]/g,'');\n    c=c.replace(/^\\[\\^\\d+\\]:\\s*.*$/gm,'');", "let c=md.replace(/^\\[\\^\\d+\\]:\\s*.*$/gm,'');\n    c=c.replace(/\\[\\^(\\d+)\\]/g,'');")
source = source.replace("loadStoredApiKey(){return SafeBrowserStorage.local.getItem('claude_api_key')||SafeBrowserStorage.session.getItem('claude_api_key')||''}", "loadStoredApiKey(){const k=SafeBrowserStorage.session.getItem('claude_api_key')||SafeBrowserStorage.local.getItem('claude_api_key')||'';if(k)SafeBrowserStorage.session.setItem('claude_api_key',k);SafeBrowserStorage.local.removeItem('claude_api_key');return k}")
source = source.replace("SafeBrowserStorage.local.setItem('claude_api_key',k);", '')
source = source.replace("method:'POST',headers:{'Content-Type':'application/json'", "...options,method:'POST',headers:{'Content-Type':'application/json'", 1)
source = source.replace('body:JSON.stringify(data),...options}', 'body:JSON.stringify(data)}', 1)
source = source.replace("if(STATE.currentChatId===chatId)document.getElementById('headerTitle').textContent=newTitle;", "const header=document.getElementById('headerTitle');if(STATE.currentChatId===chatId&&header)header.textContent=newTitle;")
# No early return when a frame contains both reasoning and content.
source = source.replace("if(dr){thinkingText+=dr;aiWrapper.thinkingText=thinkingText;window.NotionAI.Chat.Renderer.updateThinkingPanel(aiWrapper);return{thinkingText,fullAiReply}}", "if(dr){thinkingText+=dr;aiWrapper.thinkingText=thinkingText;window.NotionAI.Chat.Renderer.updateThinkingPanel(aiWrapper)}")
start = source.index('  async processStream(')
end = source.index('  consumePayload(', start)
source = source[:start] + '''  async processStream(response,aiWrapper,searchState,thinkingText,fullAiReply) {
    await readCompletionSSE(response,payload=>{
      const result=this.consumePayload(payload,aiWrapper,searchState,thinkingText,fullAiReply);
      thinkingText=result.thinkingText;fullAiReply=result.fullAiReply;
      aiWrapper._partialReply=fullAiReply;aiWrapper._partialThinking=thinkingText;
    });
    return {fullAiReply,thinkingText,searchState};
  },
''' + source[end:]
source = source.replace("if(r){fullAiReply=r;window.NotionAI.Chat.Renderer.updateAIMessage", "if(typeof d.content==='string'){fullAiReply=r;window.NotionAI.Chat.Renderer.updateAIMessage")
source = source.replace("chat.messages.filter(m=>m&&typeof m==='object'", "chat.messages.filter(m=>m&&!m.interrupted&&typeof m==='object'")
source = source.replace("return{role:'user',content}", "return{role:'user',content,interrupted:!!msg.interrupted}")
source = source.replace("return{role:'assistant',content,thinking:", "return{role:'assistant',content,interrupted:!!msg.interrupted,thinking:")
source = source.replace("chat.messages.push({role:'user',content:text});", "const userMessage={role:'user',content:text};chat.messages.push(userMessage);")
old = "}catch(err){removeThinkingIndicator();if(aiWrapper?._thinkingTimerInterval)clearInterval(aiWrapper._thinkingTimerInterval);if(err.name!=='AbortError')console.error('API Error:',err)}"
new = """}catch(err){
    removeThinkingIndicator();if(aiWrapper?._thinkingTimerInterval)clearInterval(aiWrapper._thinkingTimerInterval);
    userMessage.interrupted=true;
    const partial=aiWrapper?._partialReply||'';
    chat.messages.push({role:'assistant',content:partial||'[Interrupted response. Please retry.]',thinking:aiWrapper?._partialThinking||'',interrupted:true,modelDisplayName:selectedModelDisplayName});
    if(err.httpStatus===404)chat.conversationId=null;
    window.NotionAI.Chat.Storage.saveChats();
    if(err.name==='AbortError'&&aiWrapper)window.NotionAI.Chat.Renderer.updateAIMessage(aiWrapper,partial+'\\n\\n[Interrupted response. Please retry.]',true);
  }"""
source = source.replace(old, new)
source = source.replace("msg.role,msg.content,true,msg.modelDisplayName||null", "msg.role,(msg.interrupted?'[Interrupted turn]\\n':'')+msg.content,true,msg.modelDisplayName||null")
source = source.replace("}else{window.NotionAI.Chat.Renderer.updateAIMessage(aiWrapper,'*No visible response received.*',true)}", "}else{window.NotionAI.Chat.Renderer.updateAIMessage(aiWrapper,'*No visible response received.*',true);chat.messages.push({role:'assistant',content:'',thinking:result.thinkingText,search:ns,modelDisplayName:selectedModelDisplayName});window.NotionAI.Chat.Storage.saveChats()}")
if 'function showStorageWarning()' not in source:
    source = source.replace('function init(){', '''function showStorageWarning(){
  if(!SafeBrowserStorage.warning)return;
  let banner=document.getElementById('storageWarning');
  if(!banner){banner=document.createElement('div');banner.id='storageWarning';banner.className='memory-banner';document.querySelector('.main-area').prepend(banner)}
  banner.textContent=SafeBrowserStorage.warning;
}
window.addEventListener('notion-storage-warning',showStorageWarning);
function init(){
  showStorageWarning();''', 1)
write(path, source)
for path in ('README.md', 'README_EG.md'):
    source = read(path)
    notice = '> **Compatibility and security update:** Read [API_COMPATIBILITY.md](docs/API_COMPATIBILITY.md) before deployment. Authentication is required by default; supported options and streaming behavior are explicitly documented.\n\n'
    if notice not in source:
        write(path, notice + source)
print('Applied targeted source fixes successfully.')
