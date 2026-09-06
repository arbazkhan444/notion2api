const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('frontend/index.html', 'utf8');

function storage() {
  const data = new Map();
  return {getItem: k => data.get(k) ?? null, setItem: (k,v) => data.set(k,v), removeItem: k => data.delete(k)};
}
function browser() {
  const window = {localStorage: storage(), sessionStorage: storage()};
  const context = vm.createContext({window, URL, TextDecoder});
  vm.runInContext(fs.readFileSync('frontend/safe-browser.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('frontend/stream-reader.js', 'utf8'), context);
  return {window, context};
}
function response(text, bytewise=false) {
  const bytes = new TextEncoder().encode(text);
  const chunks = bytewise ? Array.from(bytes, b => new Uint8Array([b])) : [bytes];
  let offset = 0;
  return {body: {getReader: () => ({read: async () => offset < chunks.length ? {value: chunks[offset++], done:false} : {done:true}, cancel:async()=>{}, releaseLock(){}})}};
}

test('served inline scripts all parse', () => {
  for (const match of html.matchAll(/<script>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
});
test('corrupt chat storage does not crash initialization', () => {
  const {window} = browser();
  window.localStorage.setItem('claude_chats', '{bad json');
  assert.equal(window.SafeBrowserStorage.loadChats().length, 0);
  assert.match(window.SafeBrowserStorage.warning, /could not be read/);
});
test('storage quota failures are contained and reported', () => {
  const {window} = browser();
  window.localStorage.setItem = () => {throw new Error('QuotaExceededError')};
  assert.equal(window.SafeBrowserStorage.local.setItem('claude_chats', '[]'), false);
  assert.ok(window.SafeBrowserStorage.warning);
});
test('search navigation allows only HTTP(S)', () => {
  const {window} = browser();
  for (const url of ['javascript:alert(1)', 'data:text/html,test', 'file:///etc/passwd']) assert.equal(window.SafeBrowserStorage.safeUrl(url), '');
  assert.equal(window.SafeBrowserStorage.safeUrl('https://example.com/a'), 'https://example.com/a');
});
test('SSE accepts split CRLF and multibyte characters', async () => {
  const {window} = browser();
  const payload = JSON.stringify({choices:[{delta:{content:'你好'},finish_reason:null}]});
  const finish = JSON.stringify({choices:[{delta:{},finish_reason:'stop'}]});
  const seen=[];
  await window.readCompletionSSE(response(`data: ${payload}\r\n\r\ndata: ${finish}\r\n\r\ndata: [DONE]\r\n\r\n`, true), p=>seen.push(JSON.parse(p)));
  assert.equal(seen[0].choices[0].delta.content, '你好');
});
test('SSE EOF without completion is an error', async () => {
  const {window} = browser();
  await assert.rejects(window.readCompletionSSE(response('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'), ()=>{}), /before the response completed/);
});
test('SSE upstream error is not a completed assistant reply', async () => {
  const {window} = browser();
  await assert.rejects(window.readCompletionSSE(response('data: {"error":{"message":"failed"}}\n\n'), ()=>{}), /failed/);
});
test('SSE DONE without finish status is rejected', async () => {
  const {window} = browser();
  await assert.rejects(window.readCompletionSSE(response('data: [DONE]\n\n'), ()=>{}), /completion status/);
});
test('one delta can update both reasoning and content', () => {
  const {window, context} = browser();
  window.NotionAI={Chat:{Renderer:{updateThinkingPanel(){},updateAIMessage(){}}}};
  const start=html.indexOf('window.NotionAI.Chat.Streaming = {');
  const end=html.indexOf('/* ===== UI: THEME ===== */', start);
  vm.runInContext(html.slice(start,end), context);
  const result=window.NotionAI.Chat.Streaming.consumePayload(JSON.stringify({choices:[{delta:{reasoning_content:'reason',content:'answer'}}]}), {}, {}, '', '');
  assert.equal(result.thinkingText,'reason');
  assert.equal(result.fullAiReply,'answer');
});
