# API compatibility and review fixes

This is a text-only adapter to a private, changeable upstream protocol, not a complete OpenAI implementation. Validate against a dedicated Notion test account before deploying this branch.

## Supported contract

- Public model IDs and documented aliases; model mapping is idempotent at the transport boundary.
- Ordered system/developer, user, assistant, tool-call and tool-result context. No silent tool-result truncation.
- Function tools with JSON-object arguments validated against the supplied schema; local JSON Schema references are supported. Remote schema retrieval is forbidden.
- `tool_choice` (`auto`, `none`, `required`, or a named function), `parallel_tool_calls`, text stop sequences, and validated JSON response formats.
- Text message parts only. Images, audio, legacy request-level `functions`, multiple choices, sampling settings, and exact token limits are rejected with a clear 400 rather than silently ignored. In particular, omit `temperature`, `top_p`, `max_tokens`, and `max_completion_tokens`.

## Streaming and failures

All normal events contain OpenAI `choices`. Optional web metadata uses the same envelope. Upstream failure emits an `error` event without a successful `finish_reason`; no error is turned into an `attempt_completion` call. HTTP errors before streaming remain HTTP errors.

The upstream can replace generated text. Ordinary OpenAI clients therefore receive finalized answer text after buffering; reasoning can stream separately. The bundled web client supports body replacement. Agent output is buffered until its tool calls are fully validated. This deliberately favors correctness over time-to-first-answer-token.

The private upstream's full termination protocol and all provider-specific patch variants still require live integration validation. Network interruption exceptions are handled; an upstream that silently terminates a syntactically valid stream without an explicit failure may require further protocol-specific detection.

## Stateful conversations

Complete turns are allocated inside SQLite write transactions. Imports reconcile full history or an overlapping history window; conflicting edits require a new conversation ID. Archive rows are retained. Legacy missing window rows are backfilled, but previously overwritten/conflicting archive turns cannot be automatically reconstructed with certainty.

Only reuse an upstream thread when its stored model, account, and workspace match. Existing bindings without owner metadata are recreated. The configured single-worker server serializes turns within a conversation; SQLite protects round allocation across processes, but multi-worker deployments need a distributed generation lock and shared rate-limit storage.

Compression has one pipeline, a recoverable lease, atomic summary publication, and a truthful degraded-memory indicator. Without a working summarizer, only the configured recent window is injected; archived history remains stored. This is bounded context, not unlimited memory.

## Deployment changes

Set a nonempty `API_KEY`. Unauthenticated use requires explicit `ALLOW_UNAUTHENTICATED=true` and should be restricted to a trusted local environment. The Docker host port binds to loopback by default. The service still has one shared trust boundary; it is not a tenant-isolated hosting service.

`REQUESTS_PER_MINUTE` (default 20), `MAX_CONCURRENT_REQUESTS` (4), and `MAX_REQUEST_BYTES` (1048576) are enforced per process. Streaming requests occupy a concurrency slot until closed. Configure trusted proxies explicitly; the limiter does not trust arbitrary forwarding headers.

The summarizer uses `SILICONFLOW_API_KEY` only with SiliconFlow's API. `SILICONFLOW_MODEL` selects the model (default `Qwen/Qwen3-8B`). Conversation text is sent to that provider when summarization is enabled. Browser storage is best-effort and reports persistence failures; browser API keys are session-only.

Conversation deletion removes local SQLite data only. Upstream thread retention is separate; do not represent a local deletion as deletion from Notion. Use a least-privileged dedicated upstream account and an explicit retention policy before serving other users.

## Verification

The new regression suite imports the actual patched application. It uses fake accounts, mocked transports, and temporary SQLite databases; it must not call production Notion or summarizer endpoints. GitHub Actions runs Python and browser-logic checks. A passing mocked suite is not proof that the private upstream accepts every payload.
