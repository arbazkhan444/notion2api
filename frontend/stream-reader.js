/* SSE framing with split CRLF/UTF-8 support and explicit completion checks. */
(function (root) {
  root.readCompletionSSE = async function (response, onPayload) {
    if (!response.body) throw new Error('Response has no stream body.');
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8', {fatal: true});
    let buffer = '', sawFinish = false, sawDone = false;
    function event(block) {
      const data = block.split(/\r\n|\r|\n/).filter(line => line.startsWith('data:'))
        .map(line => line.slice(5).replace(/^ /, '')).join('\n');
      if (!data) return;
      if (data.trim() === '[DONE]') {
        if (!sawFinish) throw new Error('Stream ended without a completion status.');
        sawDone = true;
        return;
      }
      let parsed;
      try { parsed = JSON.parse(data); } catch (_) { throw new Error('Malformed SSE JSON.'); }
      if (parsed.error) throw new Error(parsed.error.message || 'Upstream stream failed.');
      if (parsed.choices?.some(choice => choice.finish_reason != null)) sawFinish = true;
      onPayload(data);
    }
    try {
      while (!sawDone) {
        const {done, value} = await reader.read();
        if (done) { buffer += decoder.decode(); break; }
        buffer += decoder.decode(value, {stream: true});
        let match;
        while ((match = /\r\n\r\n|\n\n|\r\r/.exec(buffer))) {
          const block = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          event(block);
          if (sawDone) break;
        }
      }
      if (!sawDone) throw new Error('Connection closed before the response completed.');
    } finally {
      try { await reader.cancel(); } catch (_) { /* Already closed or aborted. */ }
      reader.releaseLock();
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
