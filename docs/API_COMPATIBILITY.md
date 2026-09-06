# API compatibility and review fixes

This is a text-only adapter to a private, changeable upstream protocol, not a complete OpenAI implementation. Validate against a dedicated Notion test account before deploying this branch.

## Supported contract

- Registry model IDs and documented aliases; model mapping is idempotent at the transport boundary. Registry names do not guarantee a model is available to your account.
- Ordered user/assistant/tool context; system/developer content is retained as instructions in the upstream transcript. This is an adaptation, not native OpenAI role isolation.
- Function tools with JSON-object arguments checked against the supplied schema. Local JSON Schema references work; remote schema retrieval is forbidden.
- `tool_choice` (`auto`, `none`, `required`, or a named function), `parallel_tool_calls`, text stop sequences, and validated JSON-object response formats.
- Text message parts only. Images, audio, legacy request-level `functions`, multiple choices, sampling settings, and exact token limits are rejected rather than silently ignored. Omit `temperature`, `top_p`, `max_tokens`, and `max_completion_tokens`.
- Token usage is not measured; existing zero-valued usage fields must not be used for billing or budget enforcement.

## Streaming and failures

Normal events have OpenAI `choices`; optional web metadata uses that envelope too. Failure emits an `error` event without a successful `finish_reason`. Errors and tool failures are never converted into fabricated `attempt_completion` calls. Failures before streaming remain HTTP errors.

The upstream can replace both generated answer and reasoning text. Ordinary OpenAI clients therefore receive finalized text after buffering. The bundled web client accepts explicit answer/reasoning replacements and can display provisional output. Agent output is buffered until its complete function calls have been validated. This favors correctness over time-to-first-answer-token; it is not token-for-token pass-through streaming.

The private upstream's full termination protocol and all provider-specific patch variants still require live validation. Network interruption exceptions are handled; a syntactically valid silent EOF without an explicit error may require more protocol-specific detection. Record maps without current-response correlation remain fallback data rather than authoritative replacements.

## Stateful conversations

Complete turns are allocated inside SQLite write transactions. Imports reconcile full history or an overlapping client window; conflicting edits require a new conversation ID. Archive rows are retained. Legacy missing window rows are backfilled; ambiguous archive collisions stop reuse instead of silently selecting a turn. Back up the SQLite database before upgrading. Previously overwritten data cannot be reconstructed with certainty.

An upstream thread is reused only when its stored account, workspace, and model match. Ownerless old bindings are recreated. The single-worker service serializes generation within a conversation. SQLite protects allocation across processes, but multiple workers still need distributed generation locks, shared quotas, and coordinated compression.

Compression uses a per-conversation pipeline, recoverable leases, and atomic summary publication. Pending/failed raw turns stay available in the recent window. Without a working summarizer, the bounded recent window is injected and memory degradation is reported; the full archive is retained. This is bounded context, not unlimited memory.

Completed upstream responses may be saved before the client receives the final network bytes. This API does not provide exactly-once delivery or request-level idempotency keys; inspect conversation state before replaying an interrupted stateful request.

## Deployment and privacy

Set a nonempty random `API_KEY`. Unauthenticated use requires explicit `ALLOW_UNAUTHENTICATED=true` and should remain trusted-local only. Docker host ports bind to loopback by default. CORS defaults to local origins, not `*`. Deployment environment values override `.env`; `NOTION_ACCOUNTS` overrides `accounts.json`.

`REQUESTS_PER_MINUTE` (default 20), `MAX_CONCURRENT_REQUESTS` (4), and `MAX_REQUEST_BYTES` (1048576) apply per process. A stream occupies its concurrency slot until the downstream application closes. The limiter uses the ASGI client address, not arbitrary forwarding headers. Configure the server's trusted proxies explicitly when deploying behind a proxy.

There is one shared trust boundary, not tenant isolation. Authenticated clients share the configured account pool and conversation store. Read-only mode is requested upstream but is not a substitute for least-privilege accounts and actual Notion permissions. Do not expose this service to untrusted users.

Summarization sends older conversation text to SiliconFlow when `SILICONFLOW_API_KEY` is configured. That key is used only with SiliconFlow. `SILICONFLOW_MODEL` defaults to `Qwen/Qwen3-8B`. No fallback provider receives it.

Local conversation deletion removes SQLite data only; Notion thread retention is separate. Define retention and backup policies before storing sensitive data. Application logs use metadata rather than prompts or credentials. Dependency versions have bounds, not a fully resolved lockfile or a completed vulnerability audit.

## Browser implementation

The original layout/template is preserved. `app/frontend_view.py` serves both `/` and `/index.html`, wraps boot-time storage access, and loads `safe-browser.js`, `stream-reader.js`, and `app-fixes.js` before initialization. The older `frontend/js` copies are not the served application's authority. A changed template marker fails startup rather than silently dropping this safety layer.

Browser API keys migrate to session-only storage. Chat persistence is best-effort with visible warnings; corrupt chat data is not overwritten during recovery. Interrupted turns retain partial output and are excluded from subsequent successful-history submissions. Search navigation accepts only HTTP(S). Ordinary Markdown link labels are preserved.

## Verification status

**Tests are committed but have not been executed for this branch.** GitHub MCP accepted source commits but returned HTTP 404 for attempts to add workflow files. No Actions workflow or successful application-test result was produced. Prior excerpt/SQL checks from the review are not validation of these fixes.

The suites import the actual application and use fake accounts, mocked transports, temporary SQLite databases, and browser logic fixtures. To validate in an authorized environment, with Python 3.11+ and Node 20+:

```bash
python -m pip install -r requirements.txt
python -m compileall -q app main.py tests
python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
```

Before removing draft status:

- [ ] Run all checks above and review failures.
- [ ] Validate migrations against a backed-up copy of a real legacy database.
- [ ] Exercise all model/provider transcript and patch variants with a dedicated test account.
- [ ] Exercise browser rename, storage denial/quota, cancellation, and reconnect behavior.
- [ ] Verify proxy authentication, limits, thread ownership, and retention settings.
- [ ] Audit and lock deployment dependencies.
