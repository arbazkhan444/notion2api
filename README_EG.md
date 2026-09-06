# Notion2API

> Notion AI → a limited, text-only OpenAI-compatible API

🌐 English | [中文](./README.md)

Notion2API adapts Notion AI's private web interface to `/v1/chat/completions`. The private protocol and model availability can change. It is not a complete OpenAI implementation or a multi-tenant hosting service.

> **Review-fix branch:** source changes and regression tests are committed, but tests and live upstream integration have **not been executed**. Read [API compatibility and validation](docs/API_COMPATIBILITY.md) before deploying. Back up existing SQLite data and validate in a dedicated test environment first.

## Features and modes

- Text streaming/non-streaming, ordered conversation context, and strict function-call/schema validation.
- Account pool with cooldowns and account/workspace-bound thread reuse; 429 responses are respected, not bypassed by rotation.
- SQLite atomic complete turns, archive-preserving migration, and recoverable summarization in Heavy mode.
- Built-in chat UI with dark mode, rename/star/delete, reasoning and search panels when available, and interrupted-response recovery.

| Mode | Context | Database | Reasoning/search UI |
|---|---|---|---|
| Lite | Last user prompt and supplied instructions | None | No |
| Standard | Client-supplied ordered history | None | When upstream provides it |
| Heavy | Recent window plus summaries; full archive retained | SQLite | When upstream provides it |

The example `.env` selects `standard`; the code fallback is `heavy`. All modes use the same configurable process-local request limits (default 20 requests/minute, 4 concurrent requests, 1 MiB request body). Tool requests use a stateless agent adapter regardless of the configured mode.

## Setup

### 1. Configure an account privately

Install the requirements, then use the existing local login helper:

```bash
python -m pip install -r requirements.txt
python login.py
python login.py --check
```

Other helper options include `--list`, `--manual`, and `--profile work`. Alternatively, use your browser's developer tools and the repository's `scripts/extract_notion_info.js` to configure your own account.

`accounts.json` must contain a nonempty array with `token_v2`, `space_id`, and `user_id`. Additional profile fields are optional. These credentials grant access to the configured Notion account: keep them private, use least privilege, and do not commit them. Both `.env` and `accounts.json` are ignored by Git.

### 2. Configure service authentication

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the generated value in `.env` as `API_KEY`, and use that **same value** in clients. An arbitrary non-matching key no longer works. Empty keys fail startup unless `ALLOW_UNAUTHENTICATED=true` is explicitly enabled for trusted-local use.

```env
API_KEY=replace-with-your-generated-key
APP_MODE=standard
HOST=127.0.0.1
PORT=8000
```

Deployment environment values take precedence over `.env`. If set, `NOTION_ACCOUNTS` takes precedence over `accounts.json`; remove a stale environment override before relying on the file.

For optional Heavy-mode summarization, set `SILICONFLOW_API_KEY` and optionally `SILICONFLOW_MODEL` (default `Qwen/Qwen3-8B`). This sends older conversation turns to SiliconFlow. Without a working summarizer, only the recent window is injected and degraded memory is reported; the raw archive remains stored.

### 3. Start one worker

```bash
uvicorn app.server:app --host 127.0.0.1 --port 8000 --workers 1
```

Or, after configuring `.env` and `accounts.json`:

```bash
docker compose up --build -d
```

Docker binds the **host** port to `127.0.0.1` by default and the container listener to `0.0.0.0`. Public access requires an explicit `HOST_BIND` override plus appropriate TLS/authentication and network controls. Run one worker: conversation-generation locks and quotas are process-local.

Open `http://localhost:8000` for the UI. Use the running application, not the raw `frontend/index.html`: the server supplies its shared browser-safety layer.

## API usage

Suggested client settings:

```text
Provider: OpenAI Compatible
Base URL: http://localhost:8000/v1
API Key: the exact configured API_KEY
Model: claude-sonnet4.6
```

Example using the optional OpenAI Python SDK, with `API_KEY` exported in your shell:

```python
import os
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key=os.environ["API_KEY"])
response = client.chat.completions.create(
    model="claude-sonnet4.6",
    messages=[{"role": "user", "content": "Hello"}],
    stream=True,
)
for chunk in response:
    print(chunk.choices[0].delta.content or "", end="")
```

Do not send unsupported `temperature`, `top_p`, `max_tokens`, or `max_completion_tokens`; these return 400 instead of being silently ignored. Images, audio, multiple choices, and legacy request-level `functions` are not supported. Function calls must satisfy supplied schemas and `tool_choice`; invalid upstream output becomes an error, not a fabricated successful completion.

Normal OpenAI clients receive finalized answer/reasoning text after buffering because upstream text can be replaced. The built-in UI supports provisional output and replacements. No fixed first-token latency is promised. Token usage is not measured.

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/chat/completions` | POST | Text completions and function-call adaptation |
| `/v1/models` | GET | Registry model IDs; authentication required |
| `/v1/conversations/{id}` | DELETE | Local SQLite history only |
| `/health` | GET | Pool availability and uptime |
| `/` | GET | Built-in UI |

## Model registry

Current IDs: `claude-sonnet4.6`, `claude-sonnet5`, `claude-opus4.6`, `claude-opus4.7`, `claude-opus4.8`, `gpt-5.2`, `gpt-5.4`, `gpt-5.5`, `gemini-2.5flash`, `gemini-3.1pro`, `kimi-2.6`, `grok-4.3`, `grok-build0.1`, `deepseek-v4pro`.

Availability and labels depend on the upstream; this list does not attest to vendor capabilities. Existing aliases including `gpt-4o`, `gpt-4.1`, and `claude-sonnet-4` map to `claude-sonnet4.6`. Unknown IDs are rejected rather than silently remapped.

## Operational notes

- Authenticated clients share one configured account pool and history store. This is **not tenant isolation**. A read-only upstream flag is not a substitute for actual account permissions.
- Respect `Retry-After` on 429. Refresh expired credentials locally; 401/403 accounts are disabled in the current process until configuration/restart.
- Browser keys are session-only; corrupt chat storage is preserved, not overwritten. Persistence failures produce a warning.
- Deleting local conversations does not confirm deletion of Notion threads or backups.
- Docker health checks mark containers unhealthy; `restart: always` does not by itself restart a still-running unhealthy process.
- Reverse proxies must disable response buffering, allow sufficient upstream timeouts, and configure trusted forwarding addresses deliberately.

See [.env.example](.env.example) for request limits, local CORS defaults, paths, and provider settings. The old `DISABLE_RATE_LIMIT` setting is not supported.

## Validation

```bash
python -m compileall -q app main.py tests
python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
```

Python 3.11+ and Node 20+ are recommended. The suites use fake credentials and mocked transports. They are **not yet run on this branch**: connected GitHub workflow-file writes returned HTTP 404. No passing CI result is claimed. Provider variants, browser behavior, legacy migrations, and deployment dependencies require validation before merge; dependency bounds are not a lockfile or vulnerability audit.

## License and attribution

MIT License. Original project: [maverickxone/notion2api](https://github.com/maverickxone/notion2api).

*The original project was built with assistance from Claude Code.*
