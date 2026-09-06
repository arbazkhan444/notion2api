# Notion2API

> Notion AI → 有限的、仅文本 OpenAI 兼容 API

🌐 [English](./README_EG.md) | 中文

Notion2API 将 Notion AI 的私有网页接口适配为 `/v1/chat/completions`。上游协议和模型可用性可能变化；本项目并非完整 OpenAI API，也不是具有租户隔离能力的托管服务。

> **代码审查修复分支：** 已提交代码和回归测试，但尚未执行测试或进行真实上游验证。部署前请阅读 [兼容性说明与验证清单](docs/API_COMPATIBILITY.md)，备份已有 SQLite 数据，并先在专用测试环境验证。

## 功能与模式

- 文本流式/非流式接口、有序会话上下文，以及严格的工具调用与参数校验。
- 多账号负载均衡、冷却及账号/工作区绑定的线程复用；不会通过轮换账号绕过 429 限流。
- Heavy 模式提供事务化完整轮次存储、保留原始归档的迁移和可恢复的摘要压缩。
- 内置聊天界面支持重命名、收藏、删除、深色模式，以及上游提供的思考和搜索信息。

| 模式 | 上下文 | 数据库 | 思考/搜索面板 |
|---|---|---|---|
| Lite | 最后一次用户输入及所提供的指令 | 无 | 不支持 |
| Standard | 客户端提供的有序历史 | 无 | 上游提供时支持 |
| Heavy | 最近窗口与摘要；保留完整原始归档 | SQLite | 上游提供时支持 |

`.env.example` 选择 `standard`，未设置时的代码默认值为 `heavy`。所有模式统一使用可配置的进程级限制：默认每分钟 20 次请求、4 个并发请求、1 MiB 请求体。带工具的请求走无状态 Agent 适配路径。

## 快速开始

### 1. 在本地配置账号

```bash
python -m pip install -r requirements.txt
python login.py
python login.py --check
```

登录辅助脚本还支持 `--list`、`--manual`、`--profile work`。也可通过自己浏览器的开发者工具及 `scripts/extract_notion_info.js` 配置账号。

`accounts.json` 应是非空 JSON 数组，每个账号必须包含 `token_v2`、`space_id`、`user_id`。这些凭据代表你的 Notion 账号权限，请使用最小权限账号，切勿提交、分享或发送给不可信服务。`.env` 与 `accounts.json` 均已被 Git 忽略。

### 2. 设置服务认证

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

将生成的随机值写入 `.env` 的 `API_KEY`，客户端必须使用**相同的值**，不能再随意填写其他字符串。空密钥会阻止服务启动，除非明确设置 `ALLOW_UNAUTHENTICATED=true`；该选项仅适合可信本地环境。

```env
API_KEY=replace-with-your-generated-key
APP_MODE=standard
HOST=127.0.0.1
PORT=8000
```

部署环境变量优先于 `.env`；设置 `NOTION_ACCOUNTS` 时，它优先于 `accounts.json`。使用账号文件前，请移除过期的环境变量覆盖。

Heavy 模式可选配置 `SILICONFLOW_API_KEY` 及 `SILICONFLOW_MODEL`（默认 `Qwen/Qwen3-8B`）。启用后会将较早的对话内容发送给 SiliconFlow 生成摘要。未启用或压缩失败时只注入有限的最近窗口，并报告记忆降级；原始归档仍保留。

### 3. 单进程启动

```bash
uvicorn app.server:app --host 127.0.0.1 --port 8000 --workers 1
```

或在准备好 `.env` 和 `accounts.json` 后：

```bash
docker compose up --build -d
```

Docker 默认仅将宿主机端口绑定到 `127.0.0.1`，容器内监听 `0.0.0.0`。公开访问需要明确修改 `HOST_BIND` 并配置 TLS、认证和网络控制。请使用一个 worker：会话生成锁和请求配额不是分布式的。

访问 `http://localhost:8000` 使用界面。不要直接打开原始 `frontend/index.html`，运行中的服务会为它加载统一的浏览器安全层。

## API 使用

```text
Provider: OpenAI Compatible
Base URL: http://localhost:8000/v1
API Key: 与服务端 API_KEY 完全相同
Model: claude-sonnet4.6
```

Python SDK 示例见 [英文说明](README_EG.md#api-usage)。不要发送 `temperature`、`top_p`、`max_tokens` 或 `max_completion_tokens`：当前上游适配器无法可靠支持它们，因此明确返回 400，而不是静默忽略。图片、音频、多候选输出和旧版请求级 `functions` 不受支持。

工具调用必须符合所提供的名称、JSON 对象参数、schema、`tool_choice` 和并行限制；不会凭空构造成功的 `attempt_completion` 调用。系统/开发者消息内容会被保留，但这是上游文本适配，不等同于 OpenAI 原生角色隔离。

普通 OpenAI 客户端会在上游输出最终确定后收到缓冲后的正文及思考文本，因为上游可以替换已经生成的内容。内置界面支持临时输出及替换事件。没有固定首 token 延迟保证，token 用量也未被实际计量。

| 端点 | 方法 | 用途 |
|---|---|---|
| `/v1/chat/completions` | POST | 文本补全和工具适配 |
| `/v1/models` | GET | 模型注册表；需要认证 |
| `/v1/conversations/{id}` | DELETE | 仅删除本地 SQLite 历史 |
| `/health` | GET | 账号池状态和运行时间 |
| `/` | GET | 内置界面 |

## 模型注册表

当前 ID：`claude-sonnet4.6`、`claude-sonnet5`、`claude-opus4.6`、`claude-opus4.7`、`claude-opus4.8`、`gpt-5.2`、`gpt-5.4`、`gpt-5.5`、`gemini-2.5flash`、`gemini-3.1pro`、`kimi-2.6`、`grok-4.3`、`grok-build0.1`、`deepseek-v4pro`。

是否可用取决于实际账号与上游，这些标签不是对模型供应商能力的保证。已有别名 `gpt-4o`、`gpt-4.1`、`claude-sonnet-4` 等仍映射到 `claude-sonnet4.6`；未知 ID 会被拒绝，不再静默降级。

## 运维与隐私

- 所有通过认证的客户端共享账号池和会话存储，**并无租户隔离**。请求上游只读模式不能替代真实权限控制。
- 收到 429 时尊重 `Retry-After`；凭据过期时在本地刷新。401/403 账号在当前进程中会被禁用，需要更新配置并重启。
- 浏览器密钥仅存于当前会话；损坏的聊天存储不会被自动覆盖。存储不可用或容量不足会显示警告。
- 中断的响应保留部分内容，并从后续正常历史提交中排除。
- 本地删除不代表 Notion 线程或备份已被删除，请自行制定保留策略。
- Docker 健康检查仅标记状态；`restart: always` 本身不会重启仍在运行的 unhealthy 进程。
- 反向代理需关闭响应缓冲、允许足够超时时间，并明确配置可信的转发地址。

完整配置见 [.env.example](.env.example)。旧的 `DISABLE_RATE_LIMIT` 配置不再受支持。

## 验证

```bash
python -m compileall -q app main.py tests
python -m unittest discover -s tests -v
node --test tests/frontend.test.cjs
```

建议 Python 3.11+ 和 Node 20+。测试使用假凭据、模拟网络和临时数据库。**本分支尚未执行这些测试**：连接的 GitHub 在写入工作流文件时返回 HTTP 404，因此没有成功的 CI 结果。合并前需验证各上游模型、浏览器流程和旧数据库迁移，并审计与锁定依赖；当前版本范围不是锁文件或漏洞审计结果。

## 许可证与来源

MIT License。原始项目：[maverickxone/notion2api](https://github.com/maverickxone/notion2api)。

*原始项目使用 Claude Code 辅助完成。*
