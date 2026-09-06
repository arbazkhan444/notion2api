/* Shared browser storage boundary; preserves unreadable original data. */
(function (root) {
  let warning = '', chatsReadFailed = false;
  function notice(message) {
    warning = message || 'Browser storage is unavailable or full. Changes may not survive a reload.';
    if (root.document && root.CustomEvent) root.dispatchEvent(new root.CustomEvent('notion-storage-warning'));
  }
  function store(name) {
    return {
      getItem(key) { try { return root[name].getItem(key); } catch (_) { notice(); return null; } },
      setItem(key, value) {
        if (name === 'localStorage' && key === 'claude_chats' && chatsReadFailed) {
          notice('Saved chats could not be read. Export or repair browser storage before clearing it.');
          return false;
        }
        try { root[name].setItem(key, value); return true; } catch (_) { notice(); return false; }
      },
      removeItem(key) { try { root[name].removeItem(key); } catch (_) { notice(); } }
    };
  }
  const local = store('localStorage'), session = store('sessionStorage');
  root.SafeBrowserStorage = {
    local, session,
    get warning() { return warning; },
    loadChats() {
      try {
        const chats = JSON.parse(local.getItem('claude_chats') || '[]');
        if (!Array.isArray(chats)) throw new Error('Invalid chat storage');
        return chats.filter(chat => chat && typeof chat === 'object' && typeof chat.id === 'string')
          .map(chat => ({...chat, title: typeof chat.title === 'string' ? chat.title : 'Untitled chat',
                        messages: Array.isArray(chat.messages) ? chat.messages : []}));
      } catch (_) {
        chatsReadFailed = true;
        notice('Saved chats could not be read. Export or repair browser storage before clearing it.');
        return [];
      }
    },
    safeUrl(value) {
      try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; }
      catch (_) { return ''; }
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
