/* Behavior fixes for the served UI. Loaded after definitions, before DOMContentLoaded. */
(function (root) {
  const app = root.NotionAI;
  if (!app) return;
  const storage = root.SafeBrowserStorage;
  const state = app.Core?.State;

  if (state) {
    state.loadStoredApiKey = function () {
      const key = storage.session.getItem('claude_api_key') || storage.local.getItem('claude_api_key') || '';
      if (key) storage.session.setItem('claude_api_key', key);
      storage.local.removeItem('claude_api_key');
      return key;
    };
    state.persistApiKey = function (key) {
      storage.local.removeItem('claude_api_key');
      if (key) storage.session.setItem('claude_api_key', key);
      else storage.session.removeItem('claude_api_key');
    };
    state.set('apiKey', state.loadStoredApiKey());
  }

  if (app.API?.Client) {
    app.API.Client.post = async function (endpoint, data, options = {}) {
      const headers = new Headers({'Content-Type': 'application/json',
        'Authorization': `Bearer ${state.get('apiKey')}`, 'X-Client-Type': app.Core.Constants.CLIENT_TYPE});
      new Headers(options.headers || {}).forEach((value, key) => headers.set(key, value));
      return root.fetch(`${state.get('baseUrl')}${endpoint}`, {...options, method:'POST', headers, body:JSON.stringify(data)});
    };
  }

  if (app.Utils?.Validation) {
    app.Utils.Validation.sanitizeChats = function (chats) {
      if (!Array.isArray(chats)) return [];
      return chats.filter(chat => chat && typeof chat.id === 'string').map(chat => ({...chat,
        title: typeof chat.title === 'string' ? chat.title : 'Untitled chat',
        conversationId: typeof chat.conversationId === 'string' ? chat.conversationId : null,
        messages: (Array.isArray(chat.messages) ? chat.messages : [])
          .filter(message => message && ['user','assistant'].includes(message.role))
          .map(message => ({...message, content: typeof message.content === 'string' ? message.content : '',
            thinking: typeof message.thinking === 'string' ? message.thinking : '', interrupted: !!message.interrupted}))
      }));
    };
  }

  if (app.Utils?.Markdown) {
    app.Utils.Markdown._cleanFootnotes = function (text) {
      return (text || '').replace(/^\[\^\d+\]:\s*.*$/gm, '').replace(/\[\^\d+\]/g, '').replace(/\n{3,}/g, '\n\n').trim();
    };
    // Preserve ordinary link text; do not turn every hyperlink into a citation.
    app.Utils.Markdown._processCitations = function (container) {
      container.querySelectorAll('a[href]').forEach(link => {
        if (storage.safeUrl(link.getAttribute('href'))) {
          link.target = '_blank';
          link.rel = 'noopener noreferrer';
        }
      });
    };
  }

  if (app.Chat?.Renderer?.updateSearchPanel) {
    const original = app.Chat.Renderer.updateSearchPanel;
    app.Chat.Renderer.updateSearchPanel = function (wrapper) {
      if (wrapper.searchData?.sources) {
        wrapper.searchData.sources = wrapper.searchData.sources.map(source => ({...source, url:storage.safeUrl(source.url)}));
      }
      return original.call(this, wrapper);
    };
  }

  if (app.Chat?.Streaming) {
    const streaming = app.Chat.Streaming;
    streaming.processStream = async function (response, wrapper, search, thinking, reply) {
      await root.readCompletionSSE(response, payload => {
        const result = this.consumePayload(payload, wrapper, search, thinking, reply);
        thinking = result.thinkingText;
        reply = result.fullAiReply;
        wrapper._partialReply = reply;
        wrapper._partialThinking = thinking;
      });
      return {fullAiReply:reply, thinkingText:thinking, searchState:search};
    };
    streaming.consumePayload = function (payload, wrapper, search, thinking, reply) {
      if (!payload || payload === '[DONE]') return {thinkingText:thinking, fullAiReply:reply};
      const value = JSON.parse(payload);
      if (value.error) throw new Error(value.error.message || 'Upstream stream failed.');
      if (value.type === 'search_metadata') {
        this.mergeSearchState(search, value.searches || {});
        wrapper.searchData = search;
        app.Chat.Renderer.updateSearchPanel(wrapper);
      } else if (value.type === 'thinking_chunk') {
        thinking += typeof value.text === 'string' ? value.text : '';
      } else if (value.type === 'thinking_replace') {
        thinking = typeof value.thinking === 'string' ? value.thinking : '';
      } else if (value.type === 'content_replace') {
        reply = typeof value.content === 'string' ? value.content : '';
        app.Chat.Renderer.updateAIMessage(wrapper, reply, false);
      }
      const delta = value.choices?.[0]?.delta || {};
      if (typeof delta.reasoning_content === 'string') thinking += delta.reasoning_content;
      if (typeof delta.content === 'string') {
        reply += delta.content;
        app.Chat.Renderer.updateAIMessage(wrapper, reply, false);
      }
      wrapper.thinkingText = thinking;
      app.Chat.Renderer.updateThinkingPanel(wrapper);
      return {thinkingText:thinking, fullAiReply:reply};
    };
    const original = streaming.streamResponse;
    if (original) streaming.streamResponse = async function (chat, model, wrapper) {
      const visible = {...chat, messages:chat.messages.filter(message => !message.interrupted)};
      try {
        const result = await original.call(this, visible, model, wrapper);
        // The original send handler only stores visible text, so retain thinking-only turns here.
        if (!result.fullAiReply.trim()) {
          chat.messages.push({role:'assistant', content:'', thinking:result.thinkingText, search:result.searchState});
          app.Chat.Storage.saveChats();
        }
        return result;
      } catch (error) {
        const last = chat.messages[chat.messages.length - 1];
        if (last?.role === 'user') last.interrupted = true;
        const partial = wrapper._partialReply || '';
        chat.messages.push({role:'assistant', content:'[Interrupted response]\n\n' + (partial || 'Please retry.'),
                            thinking:wrapper._partialThinking || '', interrupted:true});
        if (error.httpStatus === 404) chat.conversationId = null;
        app.Chat.Storage.saveChats();
        if (error.name === 'AbortError') app.Chat.Renderer.updateAIMessage(wrapper, partial + '\n\n[Interrupted response. Please retry.]', true);
        throw error;
      }
    };
  }

  if (app.Chat?.Manager && state) {
    app.Chat.Manager.renameChat = function (id, title) {
      app.Chat.Storage.updateChatTitle(id, title);
      const header = root.document.getElementById('headerTitle');
      if (state.get('currentChatId') === id && header) header.textContent = title;
      this.renderChatList();
    };
    root.setupHeaderTitleEditing = function () {
      const header = root.document.getElementById('headerTitle');
      header.addEventListener('click', () => {
        if (state.get('isGenerating') || header.classList.contains('hidden')) return;
        const id = state.get('currentChatId');
        const chat = state.get('chats').find(chat => chat.id === id);
        if (!chat) return;
        const input = root.document.createElement('input');
        input.className = 'header-title-input'; input.value = chat.title;
        header.replaceWith(input); input.focus(); input.select();
        let closed = false;
        function finish(save) {
          if (closed) return;
          closed = true;
          input.replaceWith(header);
          const title = save ? input.value.trim() || chat.title : chat.title;
          if (save && title !== chat.title) app.Chat.Manager.renameChat(id, title);
          header.textContent = title;
        }
        input.addEventListener('blur', () => finish(true));
        input.addEventListener('keydown', event => {
          if (event.key === 'Enter' || event.key === 'Escape') {
            event.preventDefault(); finish(event.key === 'Enter');
          }
        });
      });
    };
  }

  if (root.document) {
    function warning() {
      if (!storage.warning) return;
      const parent = root.document.querySelector('.main-area');
      if (!parent) return;
      let banner = root.document.getElementById('storageWarning');
      if (!banner) { banner=root.document.createElement('div'); banner.id='storageWarning'; banner.className='memory-banner'; parent.prepend(banner); }
      banner.textContent = storage.warning;
    }
    root.addEventListener('notion-storage-warning', warning);
    root.addEventListener('DOMContentLoaded', warning);
  }
})(typeof window !== 'undefined' ? window : globalThis);
