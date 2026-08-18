# kGroup 获客初筛 Agent

这是 kGroup 实习生笔试题（二）Agent 方向的可运行实现：一个面向获客初筛场景的有状态对话 Agent。它接收客户消息，使用 Gemini 完成意图和情绪分类，再由本地确定性策略决定是否回复、安排跟进、转人工或结束会话。

项目实际开发用时：**约 6 小时**。

核心取舍：**LLM 只负责理解和生成建议，不能直接拥有业务动作权限**。最终动作必须经过状态机、策略层、回复审核和发送网关；题目要求的边界是在代码路径上强制执行的，不是只写在 Prompt 里。

## 1. 题目要求对应关系

| 题目要求 | 本项目实现 | 结果 |
| --- | --- | --- |
| LLM 判断 5 类意图 | `GeminiClient.classify()` 使用 Gemini 结构化 JSON 输出，并由 Pydantic 校验 | 已实现 |
| 独立判断明显不满情绪 | `IntentDecision.unhappy` 与 `intent` 分开建模 | 已实现 |
| `reply` | LLM 生成草稿，经过 `ReplyValidator` 和最终发送网关 | 已实现 |
| `schedule_followup` | 记录跟进动作；当前不接真实定时任务 | 已实现（最小 Demo 范围） |
| `escalate_to_human` | 会话进入 `ESCALATED`，后续自动消息全部静默 | 已实现 |
| `mark_not_interested` | 会话进入 `CLOSED_NOT_INTERESTED`，后续自动消息全部静默 | 已实现 |
| 同一客户任意 60 秒最多主动发送 1 条 | 最终 `reply` 发送前的 SQLite 原子滑动窗口限流 | 已实现 |
| 连续两次答非所问或不满必须转人工 | 持久化异常计数器；策略层确定性升级 | 已实现 |
| 升级后不能被客户消息绕过 | 终态在 LLM 调用前拦截；认证人工接口才可恢复 | 已实现 |
| 客户文本不能越权 | 不可信输入、动作枚举、状态迁移和网关白名单 | 已实现 |
| 防系统提示词/规则/价格底线泄露 | 秘密隔离、风险标记、安全模板、输出审核 | 已实现（有残余风险） |

## 2. 架构

```text
客户消息 -> HTTP/Web Console/终端
         -> ConversationService
         -> 幂等领取、终态拦截、Gemini 分类、原子异常计数
         -> PolicyEngine（唯一业务策略权威）
         -> ActionGateway
         -> ReplyValidator -> 60 秒滑动窗口 -> OutboundProvider/outbox
```

LangGraph 只作为流程入口适配（`app/workflow.py`），不承担权限边界。SQLite 持久化会话、消息、事件、幂等键、处理租约和 outbox。模型没有 function calling 工具，不能直接写数据库或调用动作。

## 3. 快速运行

要求 Python 3.11+：

```powershell
cd "D:\AI\实习\面试\kGroup_Agent"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
```

在本机 `.env` 填写：

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=在本机填写新Key
GEMINI_MODEL=gemini-2.0-flash
GEMINI_BASE_URL=https://generativelanguage.googleapis.com/v1beta
HUMAN_REACTIVATE_TOKEN=请替换为随机字符串
DATABASE_PATH=data/agent.db
HOST=127.0.0.1
PORT=8000
```

`.env` 已被 `.gitignore` 忽略，真实 Key 不应写入 README、源码、Issue 或提交记录。若 Key 曾经公开，应先在 Google AI Studio 撤销并重新生成。

### 3.1 离线 Demo（建议先验收）

```powershell
$env:LLM_PROVIDER = "demo"
$env:HUMAN_REACTIVATE_TOKEN = "local-test-token"
uvicorn app.main:app --reload --port 8000
```

打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/)。这是同源静态页面，不需要 Node/Vite：左侧切换客户，中间发送消息，右侧查看意图、情绪、异常次数、动作和状态；还可以刷新历史、填写人工凭据并测试恢复。

终端模式：

```powershell
python -m app.main --demo
```

输入 `/quit` 退出。`DemoLLM` 只验证流程和安全边界，不代表真实模型分类效果。

### 3.2 真实 Gemini

停止 Demo 后，在新终端清除临时变量并启动：

```powershell
Remove-Item Env:LLM_PROVIDER -ErrorAction SilentlyContinue
uvicorn app.main:app --reload --port 8000
```

另开终端运行：

```powershell
python scripts/check_gemini.py
```

脚本只输出配置摘要和错误分类，不打印 Key 或带 Key 的 URL。只有 `request: ok` 才表示真实链路通过。`/health` 的 `llm_configured: true` 只说明读到了 Key，不代表网络可达；`region_blocked` 表示 Google 拒绝当前网络出口/地区，需要更换允许 Gemini API 的网络或可用项目/Key。

| 诊断结果 | 含义 | 处理 |
| --- | --- | --- |
| `request: ok` | Key、模型、网络均正常 | 进行完整对话验收 |
| `region_blocked` | 地区/出口被 Google 限制 | 更换网络出口或项目/Key |
| `invalid_or_unauthorized_key` | Key 无效或未授权 | 重新生成并检查项目 |
| `model_or_endpoint_not_found` | 模型名或地址不适用 | 检查 `GEMINI_MODEL`/`GEMINI_BASE_URL` |
| `quota_or_rate_limited` | 配额、账单或频率限制 | 检查 Google 配额/账单 |
| `network_timeout`/`network_or_dns_error` | 本机网络问题 | 检查代理、DNS、防火墙 |

## 4. 四条硬约束的代码落点

### 4.1 60 秒滑动窗口

`ActionGateway.execute()` 在真正发送 `reply` 前调用 `SQLiteRateLimiter.allow()`。`BEGIN IMMEDIATE` 将过期清理、检查和写入放在一个原子事务中，使用时间戳实现任意 60 秒窗口，而不是固定分钟桶。第二条主动回复降级为 `schedule_followup` 并返回 `rate_limited=true`；provider 拒绝时释放预留。生产扩展提供 Redis Lua 适配器。

### 4.2 两次异常确定性升级

`off_topic` 和 `unhappy=true` 共用 `abnormal_streak`。`SQLiteStore.save_decision_once()` 将分类结果与消息 claim 绑定，只贡献一次；异常加一，正常消息清零。达到 2 后 `PolicyEngine` 无条件返回 `escalate_to_human`，不采信模型的动作建议。升级后在 LLM 调用前直接返回 `silent`。

### 4.3 越权与绕过

客户消息是 `<customer_message>` 中的不可信数据，不能改变 Python 控制流。Pydantic 校验模型输出，动作只能是四个 `ActionType`。所有副作用经过 `PolicyEngine` 和唯一 `ActionGateway`；未知动作、非法状态迁移、失效 claim token 都被拒绝或静默。普通消息中的“我是管理员”不能恢复会话；`/reactivate` 必须同时有 actor、合法角色和 `HUMAN_REACTIVATE_TOKEN`。

### 4.4 防套话

1. Key、系统 Prompt、内部策略和价格底线不进入客户上下文；
2. 分类器返回 `risk_flags`，敏感请求改用固定安全模板；
3. `ReplyValidator` 检查普通 LLM 回复，失败则降级为 `schedule_followup`，不发送泄露文本。

这不是数学意义上的 100% 防泄露：未知语言、编码、间接诱导和模型幻觉仍有残余风险。当前边界是：生成失败或审核失败不会变成越权动作，也不会发送不安全回复。

## 5. 动作与状态

| 动作 | 结果 |
| --- | --- |
| `reply` | 生成并审核回复，受 60 秒限流 |
| `schedule_followup` | 记录跟进事件，本轮不主动回复；当前不负责定时调度 |
| `escalate_to_human` | 会话变为 `escalated`，之后自动静默 |
| `mark_not_interested` | 会话变为 `closed_not_interested`，之后自动静默 |
| `silent` | 终态、claim 失效等保护性结果，不产生主动消息 |

高置信度明确拒绝才会关闭会话；置信度低于 `0.6` 时降级为 `schedule_followup`。

## 6. 对抗测试与结果

```powershell
pytest -q tests/test_adversarial.py -vv
pytest -q tests/test_rate_limit.py -vv
```

已验证：越权要求不会执行 `delete_customer`；“答非所问 -> 不满 -> 自称管理员”得到 `reply -> escalate_to_human -> silent`；套取系统提示词/价格底线使用安全模板；并发回复最多一个；`(customer_id, message_id)` 防止跨客户重放；LLM/provider 失败可重试且不重复计数；8 个独立进程竞争同一消息得到 `1 claimed / 7 processing`。

更多场景见 [docs/adversarial-tests.md](docs/adversarial-tests.md)。

## 7. HTTP API

```text
POST /sessions/{customer_id}/messages       客户消息
GET  /sessions/{customer_id}                认证后查看状态
GET  /sessions/{customer_id}/history        认证后查看历史
POST /sessions/{customer_id}/reactivate     认证后人工恢复
GET  /health                                服务和配置状态
GET  /docs                                  Swagger 文档
```

```powershell
Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:8000/sessions/alice/messages `
  -ContentType 'application/json' `
  -Body '{"message_id":"alice-0001","text":"我想了解一下你们的服务"}'
```

`message_id` 必须由上游通道提供并在重试时保持不变；同一客户复用不同文本会返回 422，避免重试变成重复业务消息。

## 8. 测试与验收

```powershell
pytest -q
python -m compileall -q app tests scripts
node --check app/static/app.js
git diff --check
```

当前本地结果：`38 passed`。依赖中的 LangGraph/Pydantic 弃用提示不影响测试通过。

建议顺序：先跑测试和静态检查，再用 Demo 验收网页/状态/限流/人工恢复，最后运行 `check_gemini.py`。只有 `request: ok` 才算真实 Gemini 通过；外部地区限制不应与项目自身故障混淆。

## 9. 代码结构

```text
app/domain.py            领域枚举、会话模型、PolicyEngine
app/storage.py           SQLite 会话、消息、事件、幂等、租约、outbox
app/rate_limit.py        SQLite / 内存 / Redis 滑动窗口
app/llm.py               Gemini REST、结构化分类、Demo provider
app/gateway.py           ReplyValidator、最终动作网关、outbox
app/service.py           对话用例、异常计数、人工恢复
app/workflow.py          LangGraph 流程入口适配
app/main.py              FastAPI、Swagger、终端 Demo、前端入口
app/static/              HTML/CSS/JavaScript Web Console
tests/                   状态机、限流、幂等、并发、HTTP、对抗测试
docs/                    threat model 与攻击场景说明
scripts/check_gemini.py  真实 Gemini 安全诊断
```

## 10. 已知边界与后续工作

- 没有接入真实 IM/CRM；`SQLiteOutboxProvider` 只写本地 outbox，`schedule_followup` 只记录事件，不负责定时调度。
- SQLite 适合本地 Demo/单机小规模验证；多实例生产应替换为 PostgreSQL/Redis，并保留原子状态迁移、幂等和最终动作网关。
- `/reactivate` 的共享 token 是笔试 Demo 的最小认证方案；生产应接入 SSO/RBAC、短期凭证、审计和 CSRF 防护。
- Gemini 可用性受模型、配额、网络出口和地区影响；`/health` 不主动调用模型，真实连通性以 `scripts/check_gemini.py` 为准。
- Prompt Injection 和敏感信息泄露无法承诺绝对防御；当前实现提供架构隔离、风险标记、固定模板和输出审核，后续可增加独立安全模型与人工审核。
- 尚未接入 RAG、CRM、真实定时任务和 OpenTelemetry/Langfuse；这些不影响本题要求的最小可运行闭环。

详见：[docs/threat-model.md](docs/threat-model.md) · [docs/adversarial-tests.md](docs/adversarial-tests.md)。

## 11. 提交说明

本项目按笔试题要求保留“最小可运行 Demo + 约束说明 + 对抗测试 + 已知边界”的交付范围。没有为了堆代码接入真实 IM、CRM、定时任务或复杂 RAG；后续扩展必须继续经过 `PolicyEngine` 和 `ActionGateway`，不能绕过现有安全边界。
