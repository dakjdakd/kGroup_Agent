# Guarded Lead Agent

一个面向获客初筛笔试题的、受策略控制的有状态对话 Agent。它用真实 Gemini LLM 完成意图和情绪判断，但不把业务动作权限交给模型：LLM 输出的是结构化语义信号，确定性代码负责状态、策略、权限、限流和最终副作用。

## 核心原则

> LLM 的输出是建议，不是权限。

请求链路固定为：

```text
customer message
  -> LLM structured classification
  -> deterministic abnormal streak state update
  -> PolicyEngine
  -> ReplyValidator
  -> ActionGateway (唯一副作用出口)
  -> atomic sliding-window limiter
  -> mock outbound action
```

LangGraph 只编排上述流程，不承担安全边界；模型没有任何业务工具可以调用。

## 快速启动

Python 3.11+：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
# 编辑 .env，填入 GEMINI_API_KEY
uvicorn app.main:app --reload
```

打开 `http://127.0.0.1:8000/docs` 可以使用 Swagger UI。

不配置 Key 也可以运行离线演示：

```powershell
python -m app.main --demo
```

HTTP 示例：

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/sessions/alice/messages `
  -ContentType 'application/json' `
  -Body '{"text":"我想了解一下你们的服务"}'
```

人工重新激活：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/sessions/alice/reactivate
```

## 架构和职责

| 层 | 职责 | 是否有业务副作用 |
|---|---|---:|
| GeminiClient | 意图、情绪、风险信号、回复草稿 | 否 |
| LangGraph | 编排节点 | 否 |
| PolicyEngine | 根据状态和信号确定动作 | 否 |
| State/SQLiteStore | 会话状态、异常计数、幂等、审计 | 是，持久化 |
| ReplyValidator | 回复输出审核 | 否 |
| ActionGateway | 动作白名单、状态检查、最终执行 | 是，唯一出口 |
| RateLimiter | 最终 outbound 的滑动窗口检查 | 是，发送记录 |

没有引入 Multi-Agent、MCP、RAG、Temporal 或复杂 UI：本题的复杂度来自状态和副作用约束，不是知识检索或开放式规划。这样可以让每一条硬约束都能在答辩现场指出具体代码位置并独立测试。

## LLM 输出契约

Gemini 分类器使用 JSON Schema 约束输出，并由 Pydantic 再校验：

```json
{
  "intent": "interested | needs_more_info | explicitly_rejected | off_topic | other",
  "unhappy": false,
  "confidence": 0.9,
  "reason_code": "asking_about_product",
  "risk_flags": [],
  "action_candidate": "reply | schedule_followup | escalate_to_human | mark_not_interested"
}
```

`action_candidate` 只是观测信号，不是最终动作。模型输出 `delete_customer` 等未知动作时无法通过枚举校验，也不存在对应工具。

## 四条硬约束的代码保证

### 1. 任意 60 秒最多主动一条

`ActionGateway` 是所有 `reply` 的唯一发送边界。`SQLiteRateLimiter` 在事务中清理 60 秒以前的记录、查询当前客户窗口并写入当前 action，因此不是固定分钟窗口。重复消息使用 `message_id` 幂等。

生产多实例可以安装 `redis` 后使用 `RedisRateLimiter`。它用 Redis Sorted Set 和 Lua 脚本原子完成清理、计数、写入，避免两个并发请求都看到“当前为 0”而同时发送。

### 2. 连续两次答非所问或不满必须升级

`advance_streak` 将 `intent == off_topic` 和 `unhappy == true` 合并为一个计数器，任何正常情况重置。`PolicyEngine` 在回复生成之前检查 `abnormal_streak >= 2`，强制返回 `escalate_to_human`。这条规则不依赖模型建议。

升级后 `SessionStatus.ESCALATED` 是终止自动状态；`ConversationService` 在 LLM 调用前直接返回 `silent`。只有人工接口 `/reactivate` 能改回 `ACTIVE`。

### 3. 客户文本不能越权

- 模型没有 function tools；只能返回结构化分类。
- `ActionType` 是四项枚举白名单。
- `PolicyEngine` 是动作权威，客户文本不是状态转换事件。
- `ActionGateway` 再次校验状态和动作合法性。
- `ESCALATED`/`CLOSED_NOT_INTERESTED` 下所有自动动作都被拒绝。

所以即使客户消息诱导模型输出一个未定义动作，也不会产生业务副作用。

### 4. 防系统提示词、内部规则和价格底线泄露

这不是可以承诺 100% 的自然语言安全问题，采用纵深防御：

1. Secret isolation：真实 Key、内部规则、价格底线不放入模型上下文。
2. Capability isolation：模型没有修改状态、调用工具或发送消息的能力。
3. Risk flags：分类器识别内部信息请求，策略切换到固定安全回复。
4. ReplyValidator：拦截系统提示词、内部规则、价格底线、secret 等明显泄露内容。
5. 失败降级：审核失败不发送模型文本，改为安全模板或 `schedule_followup`。

已知局限：模型可能出现未知语义泄露、幻觉或多轮诱导；输出过滤也可能漏检或误报。防御目标是让模型看不到秘密、让泄露不能改变业务状态，并降低自然语言泄露概率，而不是声称 Prompt Injection 已被数学意义上彻底解决。

## 对抗测试

运行：

```powershell
pytest -q
```

当前覆盖 9 个测试，包括：

1. 明确拒绝关闭会话；
2. `off_topic + unhappy` 连续两次升级；
3. 升级后管理员文本仍静默；
4. 正常消息重置共享异常计数器；
5. 滑动窗口阻止第二条 outbound；
6. 重复 `message_id` 不重复发送；
7. 越权文本不能产生未知动作；
8. 只有显式人工接口能恢复；
9. 并发请求最多一条 outbound，以及 59.9 秒阻止、60.1 秒允许的窗口边界。

## 目录

```text
app/
  domain.py       枚举、领域模型、异常计数、PolicyEngine
  storage.py      SQLite 会话、消息、事件、发送记录
  rate_limit.py   SQLite/内存/Redis 滑动窗口实现
  llm.py          Gemini REST 结构化客户端和离线 Demo provider
  gateway.py      ReplyValidator + 唯一副作用出口
  service.py      对话用例和人工重新激活
  workflow.py     LangGraph 编排
  main.py         FastAPI、Swagger、终端入口
tests/            状态机、限流、幂等、并发和攻击测试
```

## 安全操作说明

API Key 只从 `GEMINI_API_KEY` 环境变量读取，`.env` 已加入 `.gitignore`。不要把真实 Key 写进代码、README、Issue 或 Git 历史；如果 Key 曾经在聊天、日志或公开仓库中出现，应立即在 Gemini 控制台轮换。

## 后续演进

真实业务接入时，保留 `ActionGateway` 和 `PolicyEngine` 不变，只替换：

- SQLite 为 PostgreSQL/Redis；
- Mock outbound 为 CRM/IM adapter；
- SQLite limiter 为 Redis Lua limiter；
- 公开产品资料为受控 RAG context；
- 添加 OpenTelemetry/Langfuse exporter 和人工审核队列。

这些能力都不能绕过现有状态机和最终动作网关。
