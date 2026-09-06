/* Shared by the served entrypoint; no dependency on third-party scripts. */
(function (root) {
  let warning = '';
  function notice() {
    warning = 'Browser storage is unavailable or full. Changes may not survive a reload.';
    if (root.document && root.CustomEvent) {
      root.dispatchEvent(new root.CustomEvent('notion-storage-warning'));
    }
  }
  function store(name) {
    return {
      getItem(key) { try { return root[name].getItem(key); } catch (_) { notice(); return null; } },
      setItem(key, value) { try { root[name].setItem(key, value); return true; } catch (_) { notice(); return false; } },
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
        warning = 'Saved chats could not be read. Export or repair browser storage before clearing it.';
        return [];
      }
    },
    safeUrl(value) {
      try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; }
      catch (_) { return ''; }
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
