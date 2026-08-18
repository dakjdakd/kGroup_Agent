# Guarded Lead Agent

一个面向获客初筛场景的、受策略约束的有状态对话 Agent。

它使用 Gemini 完成意图、情绪和风险信号识别，但模型没有业务工具权限，也不能直接发送消息或改变会话状态。LLM 只提供结构化语义判断；确定性代码负责策略、状态机、限流、审计和最终动作执行。

> **LLM 的输出是建议，不是权限。**

## 为什么这样设计

```text
客户消息
   ↓
Gemini Structured Classification
   ↓
SQLite 原子状态更新 / 异常计数
   ↓
PolicyEngine（确定性策略）
   ↓
ReplyValidator（输出审核）
   ↓
ActionGateway（唯一动作副作用出口）
   ↓
滑动窗口限流 / 幂等 / 审计
   ↓
Mock outbound action
```

LangGraph 在当前版本只负责调用这个用例流程，不承担权限或安全边界。这样即使模型被提示注入、输出未知动作或判断错误，也不能绕过代码层约束。

## 功能覆盖

### 意图与动作

模型输出以下结构化字段：

```json
{
  "intent": "interested | needs_more_info | explicitly_rejected | off_topic | other",
  "unhappy": false,
  "confidence": 0.92,
  "reason_code": "asking_about_product",
  "risk_flags": [],
  "action_candidate": "reply",
  "reply_draft": "..."
}
```

最终动作不是由 `action_candidate` 直接决定，而是经过 `PolicyEngine` 和 `ActionGateway`：

| 动作 | 行为 |
|---|---|
| `reply` | 生成并审核回复，随后通过最终发送网关 |
| `schedule_followup` | 写入跟进事件，本轮不回复；当前版本不接真实定时任务 |
| `escalate_to_human` | 转人工并进入不可自动执行状态 |
| `mark_not_interested` | 关闭会话 |

### 四条代码级约束

| 约束 | 实现位置 | 保证方式 |
|---|---|---|
| 60 秒滑动窗口最多主动发送一条 | `ActionGateway`、`SQLiteRateLimiter` | `BEGIN IMMEDIATE` 原子清理、检查、写入；生产可注入 Redis Lua 适配器 |
| 连续两次 `off_topic` 或 `unhappy` 必须转人工 | `SQLiteStore.apply_abnormal_signal`、`PolicyEngine` | 两种信号共用计数器；正常消息重置；数据库事务避免并发丢计数 |
| 升级后保持静默 | `ConversationService`、`ActionGateway` | 终止状态在 LLM 调用前检查；状态迁移使用条件更新 |
| 客户文本不能越权 | `IntentDecision`、`PolicyEngine`、`ActionGateway` | Pydantic 枚举、动作白名单、状态迁移校验；模型没有 function tools |
| 防内部提示词/规则/价格底线泄露 | `llm.py`、`ReplyValidator` | 秘密不进入模型上下文；风险标记、安全模板和输出审核多层防御 |

### 状态机

```text
ACTIVE
 ├─ reply / schedule_followup ──> ACTIVE
 ├─ mark_not_interested ────────> CLOSED_NOT_INTERESTED
 └─ escalate_to_human ──────────> ESCALATED

ESCALATED / CLOSED_NOT_INTERESTED
 └─ 自动消息 ───────────────────> silent

人工 /reactivate
 └─> ACTIVE
```

## 快速启动

要求 Python 3.11+。

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
# 编辑 .env，填入 GEMINI_API_KEY
uvicorn app.main:app --reload
```

应用启动时会读取项目根目录的 `.env`；已经存在的系统环境变量优先。启动后访问 [Swagger UI](http://127.0.0.1:8000/docs)。

### 无 API Key 的离线演示

```powershell
python -m app.main --demo
```

离线演示使用 `DemoLLM`，只验证流程、状态和动作边界，不代表真实模型的分类效果。

## HTTP API

发送客户消息：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/sessions/alice/messages `
  -ContentType 'application/json' `
  -Body '{"text":"我想了解一下你们的服务"}'
```

典型返回：

```json
{
  "customer_id": "alice",
  "message_id": "...",
  "action": "reply",
  "reply": "...",
  "intent": "interested",
  "unhappy": false,
  "abnormal_streak": 0,
  "session_status": "active",
  "rate_limited": false,
  "reason": "intent_interested",
  "trace_id": "..."
}
```

其他接口：

```text
GET  /health
GET  /sessions/{customer_id}
POST /sessions/{customer_id}/reactivate
```

`/reactivate` 是 demo 的人工控制入口，生产环境还需要接入管理员身份认证和授权。

## 测试与验证

```powershell
pytest -q
python -m compileall -q app tests
```

当前测试覆盖 15 个关键不变量：

- 明确拒绝会关闭会话；
- `off_topic + unhappy` 混合连续两次会升级，升级后静默；
- 正常消息会重置共享异常计数器；
- 滑动窗口边界和并发发送限制；
- `message_id` 幂等和失败结果重放一致；
- 对话历史按入站/出站顺序传给分类器；
- 未知动作不会进入执行层；
- 回复草稿泄露敏感内容时自动降级；
- 人工重新激活、SQLite 原子写入和 `now=0.0` 边界。

## 代码结构

```text
app/
  domain.py       枚举、领域模型、PolicyEngine、异常信号
  storage.py      SQLite 会话、历史、事件、幂等和原子状态更新
  rate_limit.py   SQLite / 内存 / Redis 滑动窗口实现
  llm.py          Gemini REST 结构化客户端和离线 Demo provider
  gateway.py      ReplyValidator 与唯一动作副作用出口
  service.py      对话用例、幂等重放、人工重新激活
  workflow.py     LangGraph 入口适配
  main.py         FastAPI、Swagger 和终端演示
tests/            状态机、限流、并发、幂等和对抗测试
docs/
  threat-model.md
  adversarial-tests.md
```

## 安全边界与已知限制

- 模型不接收真实 API Key、价格底线或内部规则全文。
- 客户消息只作为不可信数据传入，不会成为工具指令。
- 所有业务动作必须经过 `PolicyEngine` 和 `ActionGateway`。
- Prompt Injection 和自然语言泄露不能承诺数学意义上的 100% 防御；当前目标是减少秘密暴露面、限制模型能力，并在输出不安全时拒绝发送。
- SQLite 适合本地 demo 和小规模验证；多实例生产部署应替换为 PostgreSQL/Redis，并保留同样的原子状态和动作网关边界。
- `schedule_followup` 当前只记录跟进事件，不负责真实定时调度。

## 后续演进

可以在不改变安全边界的前提下替换：

- SQLite → PostgreSQL / Redis；
- Mock outbound → CRM / IM adapter；
- 本地事件 → OpenTelemetry / Langfuse；
- 公开产品上下文 → 受控 RAG；
- 人工接口 → 带角色权限的审核队列。

不建议为了展示“Agent 感”加入没有明确价值的 Multi-Agent、MCP、向量数据库或复杂前端：本题的核心难点是状态一致性、动作控制和安全边界。
