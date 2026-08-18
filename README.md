# Guarded Lead Agent

面向获客初筛场景的有状态对话 Agent。Gemini 负责意图、情绪和风险信号识别；确定性代码负责策略、状态机、限流、审计和最终动作。模型没有业务工具权限。

> **LLM 的输出是建议，不是权限。**

## 架构

```text
客户消息 → Gemini 结构化分类 → 原子状态更新 → PolicyEngine
        → ReplyValidator → ActionGateway → 滑动窗口限流 → pending/accepted outbox
```

LangGraph 当前只是流程入口适配，不承担权限边界；所有业务动作都必须经过 `PolicyEngine` 和 `ActionGateway`。

## 核心约束

| 约束 | 代码保证 |
|---|---|
| 60 秒内最多主动发送一条 | `SQLiteRateLimiter` 使用 `BEGIN IMMEDIATE` 原子清理、检查、写入；另有 Redis Lua 适配器 |
| 连续两次答非所问或不满必须转人工 | `apply_abnormal_signal` 原子维护共享计数器，正常消息重置；`PolicyEngine` 强制升级 |
| 升级后保持静默 | `ESCALATED` / `CLOSED_NOT_INTERESTED` 在 LLM 调用前拦截，只有带人工身份和 token 的 `/reactivate` 可恢复 |
| 客户文本不能越权 | Pydantic 枚举、动作白名单、状态条件迁移；模型无 function tools |
| 防内部规则和价格底线泄露 | 秘密不进模型上下文；风险标记、安全模板和 `ReplyValidator` 多层防御 |

支持四个动作：`reply`、`schedule_followup`、`escalate_to_human`、`mark_not_interested`。`schedule_followup` 当前只记录跟进事件，不负责真实定时调度。

## 快速启动

要求 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
# 在 .env 中填写 GEMINI_API_KEY
uvicorn app.main:app --reload
```

应用会读取项目根目录 `.env`，已存在的系统环境变量优先。Swagger：[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)。

不要把真实 API Key 写入源码、README 或 Git。`GEMINI_API_KEY` 只从环境变量/.env 读取；如果密钥曾经粘贴到聊天、Issue 或提交记录，应立即撤销并重新生成。人工恢复还需要配置 `HUMAN_REACTIVATE_TOKEN`。

没有 API Key 时可以运行离线流程演示：

```powershell
python -m app.main --demo
```

离线 `DemoLLM` 只验证流程和安全边界，不代表真实模型分类效果。

## HTTP API

```text
POST /sessions/{customer_id}/messages
GET  /sessions/{customer_id}       # 需要 X-Actor-Role / X-Human-Token
POST /sessions/{customer_id}/reactivate  # 需要 X-Actor-Id / X-Actor-Role / X-Human-Token
GET  /health
```

示例（`message_id` 必须由上游消息平台提供并在重试时保持不变）：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/sessions/alice/messages `
  -ContentType 'application/json' `
  -Body '{"message_id":"alice-0001","text":"我想了解一下你们的服务"}'
```

如果请求超时或返回 503，使用完全相同的 `message_id` 和 `text` 重试；服务会复用已持久化的分类结果，不会重复累计异常计数，也不会生成新的业务动作幂等键。省略 `message_id` 会返回 422，这是为了避免客户端重试被误当成新消息。

可以用下面的命令确认本机 Gemini 配置并执行一次真实分类调用（不会打印 API Key）：

```powershell
@'
from app.main import _load_dotenv
from app.llm import GeminiClient
_load_dotenv()
c = GeminiClient()
print({"configured": c.configured, "model": c.model, "base_url": c.base_url})
print(c.classify("我想了解你们的服务", []).model_dump_json())
'@ | python -
```

如果返回 `Gemini HTTP 400 ... User location is not supported`，说明 Google 当前拒绝了该网络出口/地区，并非项目读取 Key 失败；可更换允许 Gemini API 的网络出口或在 Google AI Studio 创建并配置可用项目后重试。不要把 Key 粘贴到聊天、Issue 或提交记录。

返回包含 `action`、`reply`、`intent`、`unhappy`、`abnormal_streak`、`session_status`、`rate_limited` 和 `trace_id`。

## 测试

```powershell
pytest -q
python -m compileall -q app tests
```

测试覆盖状态升级与静默、正常消息重置、滑动窗口边界、并发限流、跨客户 `message_id` 隔离、LLM 失败重试、失败异常消息不重复计数、过期 claim fencing、provider 失败不伪造 outbound、低置信度拒绝、对话历史、未知动作拒绝、输出泄露降级、人工重新激活和跨连接 SQLite 原子写入。

## 代码结构

```text
app/domain.py       枚举、领域模型、PolicyEngine
app/storage.py      SQLite 会话、历史、事件、幂等和原子更新
app/rate_limit.py   SQLite / 内存 / Redis 滑动窗口
app/llm.py          Gemini REST 结构化客户端和 Demo provider
app/gateway.py      ReplyValidator、限流、OutboundProvider 与本地 outbox 出口
app/service.py      对话用例和人工重新激活
app/workflow.py     LangGraph 入口适配
app/main.py         FastAPI、Swagger、终端演示
tests/              状态机、限流、并发、幂等和对抗测试
docs/               threat model 与攻击场景
```

## 已知边界

- Prompt Injection 和自然语言泄露无法承诺数学意义上的 100% 防御；当前方案通过最小权限、上下文隔离和输出审核降低风险。
- `/reactivate` 现在要求人工 actor、角色和共享 token，并写入 actor 审计；生产环境仍应接入真正的 SSO/RBAC、短期凭证和 CSRF 防护。
- `ActionGateway` 当前把通过策略和限流的回复写入 SQLite `outbox`：本地 provider 会标记为 `accepted`，失败会标记为 `failed`；它不是第三方 IM 的投递确认。接入真实平台时应实现带稳定 `action_id` 幂等键的 provider、`pending/sending/sent/retryable` worker 和发送状态回调。
- claim token 是消息处理租约的 fencing token；租约过期后旧 worker 的完成/失败写入会被拒绝。跨进程部署仍应使用 PostgreSQL/Redis 等具备明确租约和队列能力的基础设施。
- 消息采用 `(customer_id, message_id)` 幂等键，并记录 `processing/failed/completed` 状态；LLM 超时或非法输出会释放为可重试消息，避免永久卡死。
- 服务层和 HTTP 层都限制客户文本不超过 5000 字符，ID 不超过 128 字符且禁止控制字符；SQLite 使用 `PRAGMA user_version` 标记当前 schema 版本。
- 低于 0.6 置信度的 `explicitly_rejected` 不会直接关闭客户，会降级为 `schedule_followup`。
- SQLite 适合本地/小规模验证，多实例生产应替换为 PostgreSQL/Redis，但保留原子状态和最终动作网关。
- 真实 CRM/IM、定时任务、RAG 和 OpenTelemetry/Langfuse 尚未接入，均不能绕过现有安全边界。

详见：[docs/threat-model.md](docs/threat-model.md) · [docs/adversarial-tests.md](docs/adversarial-tests.md)
