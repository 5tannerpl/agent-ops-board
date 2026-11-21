# AOB Phase 3.1 — Unified GPU Ops & LLM Observability Dashboard

**状态：实施设计 / Copilot Agent 工作说明**  
**目标分支：`OPENTELE`**  
**已验证基线：Commit `8553ad9334f12117ffa3ad9285ede23ed91588e2`；Run `run-46c7287dc7fffda6`**  
**实施原则：一个 Run、一次 Kaggle 执行、一个 Dashboard、两个内部 Tab。**

## 0. 执行摘要

保留 AOB 现有的三个侧边栏页面：`Dashboard`、`Intake Unit Detail`、`Worker Monitor`。**不添加第四个侧边栏页面**。在 `Dashboard` 的共用 Run 选择器和 Start Live Demo 控件之下，增加两个内部 Tab：

1. **GPU & Worker Ops** — 原 Scaling Dashboard 全部内容，保证外观、行为、指标及事件展示不回归。
2. **LLM Observability** — 同一次 Run 的模型、LLM Calls、Input/Output Tokens、LLM 延迟分布、任务/验证成功率、**所有真实 LLM 调用的可滚动时间轴**，并能通过 Unit ID 查看 Worker / Attempt / LLM Span / Validation / Workflow 信息。

**Tab 切换绝不触发 Kaggle 启动、重复模型推理或创建新 Run。** 两个 Tab 使用同一已选 `run_id`，共享相同生命周期状态；没有 Trace 时应准确显示“待上传/尚未采集/不完整”，不能生成伪数据。

本阶段**不引入** OpenTelemetry Collector、Tempo、Grafana，也不建新的独立 Observability 服务。复用现有 AOB 的 Streamlit、私有 FastAPI Control API、公开 HTTPS Event Ingress 的安全边界和本地持久化能力。

## 1. Phase 3.0 真实证据与边界

从已验收 Kaggle Notebook 输出：

- `SCALING DEMO RESULT: passed None`；`TRACE CHECK: PASSED`。
- `Total Spans: 308`，其中 `aob.worker.task: 100`、`chat Qwen3-4B-Instruct-2507: 100`、`aob.result.validation: 100`、Worker 级 `aob.run: 8`。
- `Unique Unit IDs: 100`，`Missing Unit IDs: 0`，`Duplicate (Unit ID, Attempt): {}`；`Run ID consistency: PASS`。
- 模型 Qwen3-4B-Instruct-2507，llama.cpp，Kaggle 双 T4 GPU；100 个任务的 Scaling Demo 通过。

**重要：** 已验证的 100 个 LLM Spans 存在于 Kaggle 的 `supervisor.log`；**尚未验证它们已被上传并持久化在本地 AOB 数据库。** 已验证的 Notebook 日志及截图不能代替 AOB API 中真实可查询的 Span 数据。旧 Run 仅在其 Kaggle Session/工件仍可访问并可安全导入时支持回放；否则用**新 Run**完成端到端验收。

另外，业务 Step Attempt、Queue Wait、最终 Unit 状态等主要来自 Kaggle SQLite `intake.db`，**不天然存在于上述 308 个 OpenTelemetry Spans**。Phase 3.1 不可假设本地 Control API 已获得 Kaggle `intake.db`；若未传输这些记录，则 Detail 页应使用已有摘要/事件及 Trace 字段，明确标示缺失数据，不应虚构六步的精确时间轴。可选择追加轻量的、受限的 Workflow Summary 导入；不得直接上传整份 SQLite 数据库。

## 2. 信息架构与页面交互

```text
Sidebar (保持不变)
├── Dashboard
│   ├── Auth0 用户 / Live Demo 启动控件（现有）
│   ├── Run selector（共用，位于 Tab 之外）
│   ├── Run status（共用）
│   └── Tabs
│       ├── GPU & Worker Ops  [现有 Scaling Dashboard，原样保留]
│       └── LLM Observability [新增]
│           ├── Run Overview
│           ├── LLM Calls Timeline (每次调用对应真实 Unit ID)
│           └── Unit Execution Detail / Trace Waterfall
├── Intake Unit Detail
└── Worker Monitor
```

交互规则：

- 打开 Dashboard 默认选 `GPU & Worker Ops`，保持现有行为；已登录及旧 Run 选择行为不变。
- 在同一 Run 上切换 Tab **只做查询和 UI 更新**，不调用 `/api/demo/start`。
- Run selector 在 Tab 外，仅一份；更换 Run 时两个 Tab 读取对应 Run 数据，清空不属于新 Run 的已选 Unit / Call。
- `LLM Observability` 页可选择 Unit、按 Worker/Unit/Attempt/状态筛选、按开始时间或耗时排序；需要覆盖全部 Calls，不只显示 Top 10。
- Call 时间条可点击，打开该 Call 的详情，展示所关联的 Agent Task Trace 及 Validation，支持一个 Unit 有多次调用/重试/修复的情况。
- 运行中（Trace 未到达或未完整）显示数据可用性和摄取状态；完成后显示 Final Batch/完整性验证结果。

## 3. 目标页面规格

### 3.1 Run Overview

至少展示：

| 指标 | 数据来源 / 计算 | 约束 |
|---|---|---|
| Model Name | LLM Span `gen_ai.request.model` | 多模型时分组，不假定唯一 |
| LLM Calls | `chat ...` / 标准 `gen_ai.operation.name=chat` Span 数 | 包括重试/修复调用，与 Unit 数分别显示 |
| Unique Units | Task Span 的唯一 `aob.unit_id` | 可能少于 Call 数 |
| Input / Output Tokens | `gen_ai.usage.input_tokens` 和 `...output_tokens` 汇总 | 展示 usage 覆盖率，缺失不能当 0 |
| LLM Avg / P50 / P95 | `end_time - start_time`，仅 LLM Inference Spans | 不混同任务 E2E P95 / Queue Wait |
| Task success / failure | 权威运行结果 / Unit 状态摘要 | 不用 HTTP 2xx 代替业务成功 |
| Validation result | 验证结果记录，必要时区分 Span 成功和业务验证通过 | 不把存在 validation span 解释为通过 |
| Trace coverage | expected Units、已摄取的唯一 Unit / Call、上传状态 | 为不完整运行给出提示 |

需要对 Run/模型/Worker 做过滤时，只显示真实返回的数据。

### 3.2 100 LLM Calls Timeline（主功能）

- 纵轴：每次**实际 LLM Span**一行，左侧显示 `Unit ID`（必须），辅以 Call #、Worker ID 和 Attempt。
- 横轴：同一 Run 的相对时间（Run Start 为零）或明确时区的 UTC 时间；真实 `start_time` 决定水平起点，真实 `end_time-start_time` 决定条长；并发跨度允许重叠。
- 100 条必须全部可浏览（滚动/分页/虚拟列表均可），默认按开始时间排序；提供按耗时排序与按 Unit、Worker、Attempt 筛选。
- Span ID 才是 Call 唯一标识；Unit ID 不是 Call 唯一标识。同一 Unit 在重试或 Validation Repair 下可有多个 Call。
- Hover/Select 显示 Model、Unit ID、Worker、Attempt、开始/结束时间、耗时、Input/Output Tokens、Trace ID、Span ID、状态/异常标记。
- **Call → Unit 关联算法：**优先从该 Span 自身的允许列表 Attribute 获取 Unit；否则按 `(trace_id, parent_id)` 在已摄取 Span DAG 中追溯至 `aob.worker.task`，继承其 `aob.unit_id`、`aob.worker_id`、`aob.attempt`。不可按发生时间或任务顺序猜测 Unit。
- 父 Span 缺失、跨进程上下文断裂或无法关联时标记 `UNATTRIBUTED`，记录完整性问题；不能隐式匹配。
- 不按 100 次硬编码指标；支持 50/100/150 和未来不同负载。

### 3.3 Unit Execution Detail & Trace Waterfall

点击 Call 后显示：Run、Unit、Attempt、Worker、Call Span ID / Trace ID、模型、时间、Tokens、Validation 关联和 Task 总耗时。绘制真实 OTel 树：

```text
aob.run (Worker process lifetime)
└── aob.worker.task [Unit ID, Attempt, Worker ID]
    ├── chat Qwen3-4B-Instruct-2507 [LLM, tokens, duration]
    └── aob.result.validation [duration]
```

业务 `NORMALIZE_INPUT → LOAD_CONTEXT → LLM_INFERENCE → VALIDATE_OUTPUT → SAVE_RESULT → HANDOFF` 可在**存在真实 Step Attempt 数据时**与 Trace 时间轴并排/叠加显示，标明它们是 Workflow DB 证据而非 OTel Spans。若这些细节未传输，仅显示已观测 Trace 部分及“Workflow step timings unavailable”。

对 Retry / Repair，分别绘制 Attempt 和 LLM 子调用，不合并不同任务运行期的根 Span。Workflow 失败和 OTel Span Status 可能不同，需保留各自语义。

## 4. 最小化技术架构

```text
Kaggle Notebook (one real Run)
  ├── llama.cpp / GPU0+GPU1
  ├── Supervisor / Workers / SQLite queue
  ├── Existing scaling metrics → signed HTTPS event flow (保持不变)
  └── OTel SDK → supervisor.log JSONL (已验证 308 Spans)
                        |
                        v
             Post-run Trace Batch Uploader
               validate & sanitize; bounded batches
                        |
                    HTTPS/TLS
                        |
                        v
     AOB Public Ingress (8011 behind Cloudflare tunnel)
       /new trace ingestion route (独立于 scaling event schemas)
       authentication, replay protection, allowlist, rate/body limits
                        |
                        v
           AOB local durable persistence
            trace_batches / trace_spans
                        |
                        v
          AOB Private Control API (8010)
          authorized read-only query endpoints
                        |
                        v
           Streamlit Dashboard (8501)
        One Run / Two Tabs / Call→Unit→Trace
```

### 4.1 Ingestion 设计约束

- 第一版在 Scaling Run 结束后一次或多次有限大小 Batch 上传，避免在推理热路径上引入外部请求；与事件传输单独计数和重试。
- 复用现有 Public Ingress 的公网上传边界，但采用**独立的 Trace 数据接口与严格 Schema**，避免修改已发布的 Scaling Event allowlist / payload contract。
- 身份校验必须基于现有受信任的 Kaggle Run 认证机制或新增经授权的 scoped token / 签名；使用传输中的 `run_id` 做权限绑定，不能仅相信 JSON 字段值。
- TLS、请求体大小/每批 Span 数量限制、速率限制、时钟/重复上传检查、幂等写入、最大属性长度、枚举 Allowlist。
- 上传仅包括必需元数据：trace/span/parent ID、name、start/end、允许列表 attributes、必要的 status 和 service metadata；拒绝/删除 prompts、原始邮件、模型完整输出、headers、cookies、secrets、任意事件正文。
- 禁止直接把 `supervisor.log` 原样作为不受限的远程文件上传，也不要开放本地 Control API 或 OTLP 端口到公网。
- Trace 上传失败不能把已成功的 Scaling 业务结果改写成失败；单独展示 `TRACE_PENDING` / `TRACE_INCOMPLETE` / `TRACE_COMPLETE` 等摄取状态。
- Kaggle Batch Runtime 结束前需要确认上传完成（有超时和失败报告），否则本地不能假定 Spans 已到达。

### 4.2 存储与完整性

优先复用本地当前数据库访问层，不强制新建 SQLite 数据库；根据现有表结构和迁移惯例实现。建议逻辑实体：

- **TraceBatch**: `(run_id, batch_id, submitted_at, accepted_span_count, rejected_count, batch_status, schema_version)`，避免整批重复。
- **TraceSpan**: `(run_id, trace_id, span_id, parent_span_id, name, start_utc, end_utc, status, allowed_attributes, resource_metadata)`。建议以 `(run_id, trace_id, span_id)` 做幂等唯一键，并加 `run_id, aob.unit_id`/相关索引。

`run_trace_summary`、`unit_trace_summary` 初期可通过查询聚合计算，不强制增加冗余表。对每个 Run 提供：已接收数、拒绝数、重复数、关联失败数、唯一 Unit 数、预期 Unit 数（如果可确定）、是否 Finalized。**不要仅依靠 Span 数量是否等于 308 判定所有未来 Run 完整。**

### 4.3 私有查询 API（建议接口语义，路径以现有路由规范为准）

- `GET /api/demo/{run_id}/trace-summary`：指标、摄取状态、覆盖率、统计。
- `GET /api/demo/{run_id}/llm-calls`：支持分页、排序、过滤；返回 Call 到 Unit 的已验证关联。
- `GET /api/demo/{run_id}/units/{unit_id}/trace`：按 Attempt 查询真实 Span Tree 与可用 Workflow 状态。
- 可选：`GET /api/demo/{run_id}/traces/{trace_id}`：打开完整 Trace。

所有查询使用现有 Auth0/session 授权及 Run 访问检查；禁止通过 URL 任意枚举其他用户/Run。不要返回 Prompt / 模型原始输出。

## 5. 实施顺序（Agent 应分阶段提交）

### Phase 3.1A — Trace Import & Ingestion

1. 审查现有 `api.public_ingress` 安全逻辑、Event 认证、运行态存储和 Notebook Launcher；确保不破坏现有路径。
2. 设计受限 Trace Batch Schema、鉴权/幂等/拒绝策略。
3. 在 Kaggle Scaling 完成后调用 Trace Export/Uploader；明确文件缺失、批量失败的诊断与重试逻辑。
4. 持久化并提供摄取完整性元数据；独立单元/安全测试。
5. 用已授权可访问的合成 Span Fixture 验证往返，不允许用假数据宣称已通过真实 Kaggle 验收。

### Phase 3.1B — Query & Correlation

1. 按 `trace_id`, `span_id`, `parent_id` 重建 DAG；关联 Task / Unit / Attempt / Worker。
2. 计算 LLM 指标、Token 覆盖率、缺失/重复/孤儿 Span。
3. 为 Run/Call/Unit 提供受认证查询接口，覆盖分页、重试、repair、无数据、部分上传和历史 Run。

### Phase 3.1C — Unified Dashboard

1. **不增加 Sidebar 第四页**。在现有 `Dashboard` 增加 `GPU & Worker Ops`、`LLM Observability` 两个 Tab。
2. 使 Start Live Demo、Run selector、Run 状态保持共用且在 Tab 外；默认 GPU Tab。
3. GPU Tab 中保留现有可见内容与自动刷新/Scaling State 行为，避免回归。
4. LLM Tab 增加 Run Overview、所有 Calls 的真实时间轴、选中 Call 的详情与 Waterfall。
5. 无 Trace / 上传未完成 / 验证失败时显示对应状态；页面切换不得触发启动。

### Phase 3.1D — Acceptance & Diagnostics

1. 本地/CI 单元、集成、授权/安全、页面交互与已有 Scaling 回归测试。
2. 执行**全新** Kaggle High Concurrency 100 Unit Demo（除非能安全导入以前 Run 的真实工件）。
3. 对照 Notebook Console 的真实 `supervisor.log`，确认摄取 100 Task + 100 LLM + 100 Validation（该无重试基线对应 308 Spans）及 Run/Unit 关联完整。
4. UI 中 100 条 LLM 时间条均有真实 Unit ID，准确的起点、耗时、Token 字段；随机抽查多条与原始 Span 比对；不因 Tab 切换重复运行。
5. 失败/重试测试：一个 Unit 多次 LLM Call、孤儿 Span、缺 token usage、重复批次、错误 Run ID、限流/401/403、数据到达顺序异常。

## 6. 明确的非目标

- 不加入 OpenTelemetry Collector、Tempo、Grafana、Jaeger 或 OTLP 公网 endpoint。
- 不重写 Worker/Inference Router/GPU Manager、扩缩容阈值、Kaggle Notebook 的 10 次 Model Validation 或 SQLite 队列。
- 不生成 100 条虚假 Demo Calls；本轮展示必须有真实遥测证据。
- 不把当前 100 Unit 任务的 E2E P95 当作 LLM Inference P95。
- 不把 `aob.run` Worker 生命周期 Span 误计为单个 LLM Call。
- 不因切换 Tab 重做 Kaggle 任务。
- 不直接上传整个 Kaggle SQLite 数据库，亦不将原始 Prompt/Response 带到前端。

## 7. 验收清单

- [ ] Dashboard 侧边栏仍为原三个页面；内部新增两个 Tab。
- [ ] 选择 Run 一次，两个 Tab 均显示同一 `run_id`，切换不启动新的 Demo。
- [ ] GPU & Worker Ops 的现有图表、状态及事件展示回归测试通过。
- [ ] 新 Run 的真实 Trace 经鉴权 HTTPS 批量写入 AOB 持久化存储，无敏感内容泄漏。
- [ ] 100 Unit 无重试基线：100 Task、100 LLM、100 Validation、8 Worker Root Spans；100 Unique Unit IDs、0 Missing、0 Duplicated Unit/Attempt。
- [ ] 100 条真实 LLM Call 的每条时间条均关联真实 Unit ID、Worker 和 Attempt；支持完整滚动/排序/筛选。
- [ ] 时间条的开始、结束、长度与 OTel Span 完全一致；并发不被画成串行。
- [ ] LLM Tokens、Avg/P50/P95 按已上传真实 Span 聚合，缺失值与覆盖率明确显示。
- [ ] 选择 Call 展示正确 Parent/Child Trace 和 Validation；Workflow 细节仅在有真实证据时展示。
- [ ] Partial / Missing / Upload failed / Auth denied / Retry/Repair 都有清楚的 UI 状态和测试覆盖。
- [ ] 新 Kaggle Run `SCALING DEMO RESULT: passed` 且 AOB Trace ingestion / Timeline 同时通过验收。

## 8. 给 Copilot Agent 的工作方式

从 `OPENTELE` 分支开始，先审查代码和测试结构，给出受影响文件与实际 API/DB 映射，**不要假设文档中建议路径已存在**。按 3.1A→3.1B→3.1C→3.1D 分步骤完成；每一步运行相关测试，报告与基线差异。提交后提供变更文件、测试证据和新 SHA。要强调仓库 Commit 成功**不等于已 Push 到 GitHub**，Kaggle 的 pinned SHA 必须可以通过远程 fetch。请勿自动覆盖本地未跟踪的 `start_aob_local.py`。
