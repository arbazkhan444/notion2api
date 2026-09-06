/* SSE line framing accepts LF, CRLF, CR, and boundaries split across reads. */
(function (root) {
  root.readCompletionSSE = async function (response, onPayload) {
    if (!response.body) throw new Error('Response has no stream body.');
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', {fatal:true});
    let buffer = '', lines = [], sawFinish = false, sawDone = false;
    function dispatch() {
      const data = lines.filter(line => line.startsWith('data:'))
        .map(line => line.slice(5).replace(/^ /, '')).join('\n');
      lines = [];
      if (!data) return;
      if (data.trim() === '[DONE]') {
        if (!sawFinish) throw new Error('Stream ended without a completion status.');
        sawDone = true;
        return;
      }
      let parsed;
      try { parsed = JSON.parse(data); } catch (_) { throw new Error('Malformed SSE JSON.'); }
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Malformed SSE object.');
      if (parsed.error) throw new Error(parsed.error.message || 'Upstream stream failed.');
      if (parsed.choices !== undefined && !Array.isArray(parsed.choices)) throw new Error('Malformed SSE choices.');
      if (parsed.choices?.some(choice => choice && choice.finish_reason != null)) sawFinish = true;
      onPayload(data);
    }
    function consume(eof = false) {
      while (!sawDone) {
        const index = buffer.search(/[\r\n]/);
        if (index < 0) return;
        const character = buffer[index];
        if (character === '\r' && index === buffer.length - 1 && !eof) return;
        const width = character === '\r' && buffer[index + 1] === '\n' ? 2 : 1;
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + width);
        if (line === '') dispatch();
        else lines.push(line);
      }
    }
    try {
      while (!sawDone) {
        const {done, value} = await reader.read();
        if (done) { buffer += decoder.decode(); consume(true); break; }
        buffer += decoder.decode(value, {stream:true});
        consume();
      }
      if (!sawDone) throw new Error('Connection closed before the response completed.');
    } finally {
      try { await reader.cancel(); } catch (_) { /* Already closed or aborted. */ }
      reader.releaseLock();
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
