# AOB Phase 3.0 — Kaggle OpenTelemetry 接入实施方案

> 版本：1.0 | 日期：2026-11-20 | 交付对象：GitHub Codespaces / Copilot 实施 Agent  
> 项目范围：**现有 AOB + Kaggle Notebook + Qwen 本地推理 + Worker/Scaling Demo + HTTPS Event Reporting**  
> **不包含 DDH 或 DOAP**；不得创建、引用或迁移这两个项目的模块、接口或数据库表。

## 0. 执行摘要

本阶段先证明：**现有 Kaggle 运行的 AOB Agent/Worker 能生成符合 OpenTelemetry 的 Trace，并用现有 `run_id` 与 AOB HTTPS 事件关联。**

- **阶段 0A（本次必做）**：在 Kaggle Worker 的真实 Python 执行进程内初始化 OpenTelemetry SDK，将 Span 导出到本进程 Console/日志。保持原 HTTPS Event Reporter、`.env`、Autoscaler、GPU 推理及 React Dashboard 不变。
- **阶段 0B（后续独立变更）**：AOB Oracle 主机部署受保护的 OTLP/HTTP Collector → Trace 后端；Kaggle 切换到网络 Exporter；AOB Run Detail 按 `run_id` 显示 Trace。**未部署接收端前不要开启 OTLP 网络出口。**
- **非目标**：Azure Application Insights Connector、MCP、Agent PaaS、多租户商业化、Grafana 全栈、重新实现 Agent、改动已稳定的扩缩容策略。

### 当前已知的事实与实施前核验

已知：Kaggle 使用 Qwen 模型和多个 Worker 运行 AOB Demo，经 HTTPS 上报事件；最近的事件投递错误已因 `.env` 拼写修正而解决，现有 Dashboard 能显示 Run 和 Scaling。**实际仓库中的文件名、函数签名和入口需由实施 Agent 搜索确认，本文示例不得机械套用。**

实施前检查：

```bash
# 在 Kaggle 使用的代码仓库根目录；按实际位置运行
rg -n 'aob_emit|send_event|scaling_demo_started|scaling_metrics_snapshot|RUN_ID|AOB_MODEL_BACKEND|subprocess|worker' .
rg -n 'llamacpp|MODEL_URL|requests\.post|httpx|urllib' .
```

记录实际：① Notebook 第 6 步启动了哪个 Python 子进程；② Agent/Worker 任务入口；③ Qwen 调用位置；④ 现有 `run_id` 的来源和传递；⑤ Event Reporter 模块及事件发送路径。**首版只在这几个已确认位置做改动。**

## 1. 系统边界与目标数据流

```text
                 现有：保持不变
 Kaggle Notebook launcher / Step 6
           |
           +--> AOB demo Python worker process -----HTTPS existing events----> AOB Event Ingress
                       |
                       +-- Worker/Agent steps
                       +-- Qwen model inference (llama.cpp HTTP)
                       |
                       +-- OpenTelemetry TracerProvider
                             |-- Span: aob.run
                             |-- Span: aob.worker.task
                             |-- Span: gen_ai inference
                             |-- Span: aob.result.validation
                             +-- ConsoleSpanExporter --> Kaggle logs (Phase 0A)

 下一阶段（不在本次实施）：
 Kaggle OTel OTLP/HTTP --> TLS + authenticated collector --> Trace storage
                                           |
                            AOB API/BFF/React Run Detail
```

**边界原则**：OTel 只观察执行，不参与是否启动/停止 Worker、扩缩容、模型路由或业务任务完成判定。OTel Exporter 异常不得导致主业务失败，也不能取代现有 HTTPS Event Reporting。

## 2. Trace/Span 数据模型

### 2.1 Resource attributes（低基数）

| 字段 | 示例 | 备注 |
|---|---|---|
| `service.name` | `aob-kaggle-agent` | 固定值 |
| `service.version` | Git commit 短 SHA | 有值才填 |
| `deployment.environment.name` | `kaggle-demo` | 固定环境标记 |
| `aob.source` | `kaggle` | 自定义 |

### 2.2 每次运行的 Span attributes

| 属性 | 示例 | 要求 |
|---|---|---|
| `aob.run_id` | `uuid` | 与 HTTPS Event Reporter 完全一致；所有顶层任务 Span 使用同一值 |
| `aob.worker_id` | `worker-2` | 仅已知时设置，不用 API Secret/设备标识 |
| `aob.task_id` | `task-004` | 已有任务 ID 时设置 |
| `aob.units` | `150` | 数值，有意义时设置 |
| `aob.scaling_mode` | `high-concurrency` | 已有值时设置 |
| `gen_ai.operation.name` | `chat` | 模型推理 Span 上设置；按实际调用方式选值 |
| `gen_ai.request.model` | `Qwen3-4B-Instruct-2507` | 按实际配置读取 |
| `gen_ai.usage.input_tokens` | 整数 | **只有准确获得时才写** |
| `gen_ai.usage.output_tokens` | 整数 | 同上 |

**不要**采集 Prompt、回复原文、Notebook URL、Kaggle Token、GitHub PAT、Auth0 Secret、`.env`、用户输入全文、HTTP Authorization 或模型请求正文。

### 2.3 Span 层次与多进程语义

目标示例（不是现有真实 Trace）：

```text
aob.run                               [aob.run_id=...]
  aob.worker.task                     [aob.worker_id=worker-1]
    chat Qwen3-4B-Instruct-2507        [gen_ai.operation.name=chat]
    aob.result.validation
```

- **同一进程内部**使用 `start_as_current_span` 自动形成父子关系。
- **多 Worker 各自独立进程**默认不会自动共享 Trace：必须通过 W3C `traceparent`/`tracestate` 传播上下文，或者第一版各自生成独立 Trace、依靠 `aob.run_id` 在后续 AOB 视图聚合。
- 第一版**推荐后者**，避免改变当前 Worker 启动协议；**不要谎称跨进程 Span 自动成为一个树**。
- 未来要串接跨进程 Span，可在任务派发消息/启动参数中使用 OTel Propagator 注入/提取；不把 Trace ID 当业务 Run ID。

## 3. 依赖与开关

仅改动 Kaggle 使用的 Python 环境/requirements（遵循仓库已有锁文件管理策略）：

```bash
python -m pip install 'opentelemetry-api>=1.30,<2' 'opentelemetry-sdk>=1.30,<2'
```

上线前应由 CI 生成并锁定实际可兼容版本，不要在生产无限制升级依赖。阶段 0B 才引入 `opentelemetry-exporter-otlp-proto-http`。

新增环境变量（**默认禁用**）：

```dotenv
AOB_OTEL_ENABLED=false
AOB_OTEL_EXPORTER=console
AOB_OTEL_SERVICE_NAME=aob-kaggle-agent
AOB_OTEL_ENVIRONMENT=kaggle-demo
# Phase 0B 才配置，当前禁止填写任意公开地址：
# AOB_OTEL_OTLP_TRACES_ENDPOINT=https://<verified-ingest-host>/v1/traces
```

确认现有 `.env` 加载顺序：**如果 Notebook launcher 用 `subprocess` 启动 Worker，要把 `AOB_OTEL_*` 随环境传入子进程；在 Notebook 父进程中初始化 OTel 不会自动注入 Worker。**

## 4. 建议新增模块：`telemetry/otel.py`

路径可根据实际工程结构调整；以下是可运行的基础实现（Console 模式）。`configure_otel` 必须在**实际执行 Agent 的 Python 进程**启动时调用一次。

```python
# telemetry/otel.py
from __future__ import annotations

import atexit
import os
from contextlib import nullcontext

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_provider: TracerProvider | None = None
_configured = False


def otel_enabled() -> bool:
    return os.getenv("AOB_OTEL_ENABLED", "false").lower() == "true"


def configure_otel() -> None:
    global _configured, _provider
    if _configured or not otel_enabled():
        return
    exporter = os.getenv("AOB_OTEL_EXPORTER", "console")
    if exporter != "console":
        raise ValueError("Phase 0A supports only AOB_OTEL_EXPORTER=console")

    resource = Resource.create({
        "service.name": os.getenv("AOB_OTEL_SERVICE_NAME", "aob-kaggle-agent"),
        "deployment.environment.name": os.getenv("AOB_OTEL_ENVIRONMENT", "kaggle-demo"),
        "aob.source": "kaggle",
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    _provider = provider
    _configured = True
    atexit.register(shutdown_otel)


def tracer():
    return trace.get_tracer("aob.kaggle", "0.1.0")


def span(name: str):
    return tracer().start_as_current_span(name) if otel_enabled() else nullcontext(None)


def shutdown_otel() -> None:
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None
```

**实施注意**：若进程内已经由其他库设置全局 TracerProvider，不要再次覆盖；先排查现有 OTel 初始化。不要在多个文件中重复初始化，`_configured` 仅保证同一 Python 进程内的简单幂等性。若大量 Span 出现，Console 输出会放大 Kaggle 日志量，调试时应降低采样或减少内部 Span。

## 5. 插桩位置与代码示例

### 5.1 在真实 Worker 入口初始化

```python
# 示意：插入到当前 Worker 启动函数中，而不是盲目创建 worker_main.py
from telemetry.otel import configure_otel, shutdown_otel

configure_otel()
try:
    run_existing_worker_loop()  # 占位：改为仓库中已经存在的调用
finally:
    shutdown_otel()
```

注意：`run_existing_worker_loop()` 是占位符，**不得作为不存在的函数提交**；应最小修改原始入口。

### 5.2 在现有 Run / Task 执行边界建立 Span

```python
from opentelemetry.trace import Status, StatusCode
from telemetry.otel import span


def execute_existing_task_with_trace(run_id: str, task_id: str, worker_id: str, execute):
    # execute 是现有任务执行 callable 的包装；接入时保留原始参数/返回类型
    with span("aob.worker.task") as current:
        if current is not None:
            current.set_attribute("aob.run_id", run_id)
            current.set_attribute("aob.task_id", task_id)
            current.set_attribute("aob.worker_id", worker_id)
        try:
            return execute()
        except Exception as exc:
            if current is not None:
                # 注意：异常内容可能带入敏感数据；生产前配置脱敏策略
                current.record_exception(exc)
                current.set_status(Status(StatusCode.ERROR))
            raise
```

必须先识别现有 `run_id` 如何传到 Worker：如果 Worker 只有 Task ID，就从已有的任务上下文安全获取 Run ID；不要新生成第二个 Run ID。若当前没有跨进程上下文，阶段 0A 可以先在已具备 run_id 的最外层工作调用处插桩，并明确记录局限。

### 5.3 在模型推理 HTTP 调用的外层建立 GenAI Span

```python
from opentelemetry.trace import Status, StatusCode
from telemetry.otel import span


def call_model_with_trace(model_name: str, call_existing_model):
    with span(f"chat {model_name}") as current:
        if current is not None:
            current.set_attribute("gen_ai.operation.name", "chat")
            current.set_attribute("gen_ai.request.model", model_name)
        try:
            # 使用项目中已有的 requests/httpx/urllib 模型调用
            return call_existing_model()
        except Exception as exc:
            if current is not None:
                current.record_exception(exc)
                current.set_status(Status(StatusCode.ERROR))
            raise
```

- 这是**手动 GenAI Span**，只代表实际包裹的模型 HTTP 调用；不能凭此宣称已支持自动 LLM/Tool Instrumentation。
- 仅在现有模型 API 返回可信 `usage` 时补上 `gen_ai.usage.*`，不要从输出字符数估算后标注为实际 Token。
- 应包裹真实一次推理调用，避免外部重试循环多次调用被记为一个单一请求；重试若需要展示，可使用独立 Span。
- 如果模型请求发生在不同 Python 子进程，模型 Span 必须在调用者进程内初始化 SDK。

### 5.4 现有 HTTPS Event Reporter 不变

```python
# 伪代码说明逻辑位置；不要修改现有 reporter schema 或上报地址
send_event("scaling_demo_started", existing_payload)
# ... 运行现有任务并产生 Span ...
send_event("scaling_metrics_snapshot", existing_payload)
```

`gpu_instance_ready`、`inference_route_changed`、`scaling_demo_started`、`scaling_metrics_snapshot` 等当前允许的事件类型、重试策略、认证、签名、计数保持原状。**不要为了让 Trace 工作而向现有事件 Schema 添加未知字段，避免 `extra=forbid` 拒绝事件。**未来若确实需要事件携带 `trace_id`，先正式扩展 allowlist/schema 并测试向后兼容。

## 6. Kaggle Notebook 实施顺序

1. 在开发分支新建 `telemetry/otel.py` 及测试文件，修改实际 Worker 启动/模型调用的最小位置。
2. 跑现有单元测试与最小模型测试，确保 `AOB_OTEL_ENABLED=false` 时行为与以前一致。
3. 提交并让 Kaggle Notebook 拉取指定 Git Commit，核对代码版本；**不要让 Notebook 自动拉不受控制的最新分支**。
4. Kaggle 安装/同步依赖，按照已有 Step 6 传入：

   ```python
   # 示意：在已有 env = os.environ.copy() 之后追加
   env.update({
       "AOB_OTEL_ENABLED": "true",
       "AOB_OTEL_EXPORTER": "console",
       "AOB_OTEL_SERVICE_NAME": "aob-kaggle-agent",
       "AOB_OTEL_ENVIRONMENT": "kaggle-demo",
   })
   # 原有 AOB_MODEL_* / Reporter 密钥环境变量继续保留
   ```

5. 使用当前已经跑通的 `Start Live Demo` / Kaggle Notebook 流程启动，先执行轻量模式，再执行扩缩容模式。
6. 核对 Kaggle Worker 日志出现 `trace_id`、`span_id`、`name`、`aob.run_id`，且 Span 覆盖真实任务而不仅是 Notebook 启动代码。
7. 核对 AOB React Live Dashboard 的 Run/Worker/GPU 指标、Event Reporter 的 success/failure 计数与关闭 OTel 前一致（允许性能开销带来轻微时延变化）。
8. 记录 Trace 示例（先脱敏），并回滚测试 `AOB_OTEL_ENABLED=false`。

### 建议的可追踪性诊断

```bash
# Kaggle Worker 运行时（使用 notebook Python 打印非敏感配置）
python -c 'import os; print("otel_enabled=", os.getenv("AOB_OTEL_ENABLED")); print("otel_exporter=", os.getenv("AOB_OTEL_EXPORTER"))'
```

上述命令运行在哪个进程环境，输出就只代表哪个进程；真正验证须由 Worker 入口输出安全的 OTel 初始化状态。**不要打印整个 `os.environ`。**

## 7. 最小自动化测试与验收标准

### 7.1 单元测试

- [ ] OTel 默认关闭，现有 Worker 行为、返回值和异常传播不变。
- [ ] 打开 OTel 可看到 Worker Span，并且 `aob.run_id` 与现有事件 Run ID 一致。
- [ ] 模型成功/失败均产生 Span，失败仍按原逻辑向上抛出。
- [ ] 多次调用初始化不会重复设置 Provider 或创建多份重复 Span。
- [ ] Span 中没有密钥、Notebook 地址、Prompt 和用户原始数据。
- [ ] SDK 初始化失败或 Exporter 失败不会中断业务：**本次实现应在测试环境验证此性质，必要时增加安全降级封装；不能仅因使用 BatchSpanProcessor 就假设完全无影响。**

### 7.2 集成/回归

- [ ] Kaggle 原有 HTTPS Reporter 事件全部正常送达。
- [ ] Live Demo 两种运行模式（实际支持的模式为准）可正常执行。
- [ ] 现有 Scaling、GPU、Run ID 显示没有回归。
- [ ] Worker 多进程时，不错误宣称单个 Trace 覆盖全部 Worker；可用相同 `aob.run_id` 关联不同 Trace。
- [ ] 关闭开关可恢复旧行为，无需回滚 AOB 控制面。

### 7.3 本次交付物

- `telemetry/otel.py`（实际路径以项目结构为准）
- Worker 真正执行路径的最小插桩改动
- Qwen 调用路径的最小 GenAI Span 改动
- Kaggle Notebook 或其配置中的开关（无硬编码密钥）
- 单元测试、回归日志与脱敏 Trace 样例
- `README` 中的启用与回滚说明

**本次完成定义：Kaggle 本地 Console Exporter 已产生可信 Trace，现有 HTTPS Event Reporter 仍正常工作。此时 AOB React 页面暂时不会显示 OTel Trace，属于预期状态。**

## 8. 后续 Phase 0B — OTLP 网络接入（本次不实施）

需要先设计 Oracle 侧受保护的数据接收端：

```text
Kaggle Worker
  -> HTTPS /v1/traces (authenticated OTLP/HTTP)
  -> trusted reverse proxy / telemetry ingress
  -> OTel Collector (private Docker network)
  -> trace backend (private, volume/retention)
  -> AOB backend read API
  -> React Run Detail
```

实现前的强制条件：

1. **独立身份验证**：不可把现有 Run Event HMAC/Token 直接假设为 OTLP Collector 原生支持；在反向代理/网关设计受支持的鉴权及凭据轮换。
2. **严格私有网络**：Collector 和 Trace Backend 不能向公网开放管理端口；限定流量、请求体大小、速率和 IP/租户策略。
3. **正确 OTLP 终点**：HTTP Protobuf Trace Exporter 一般使用 `https://HOST/v1/traces`；不能把现有 AOB JSON Event API URL 当成 OTLP Endpoint。
4. **存储与成本**：为 Trace Backend 定义 retention、容量、采样和日志脱敏；不依赖 Kaggle 会话保留数据。
5. **字段映射**：以 `aob.run_id` 做 Run 关联；一个 Run 可能对应多个 Trace，不能把 Trace ID 当 Run 主键。
6. **时间可靠性**：对于失败重试、断网和 Kaggle 会话结束，只提供尽力导出语义；禁止宣传“绝对不丢失”。
7. **兼容现有 BFF/Auth0**：Dashboard 查询 Trace 必须通过认证 BFF/后端；不要在浏览器暴露 Collector 或后端凭据。

### Phase 0B 客户端切换示意（待服务端就绪后）

```bash
python -m pip install opentelemetry-exporter-otlp-proto-http
```

将 `AOB_OTEL_EXPORTER=otlp` 作为显式新选项，新增经过验证的 TLS Endpoint 与机密 Header；**扩展 `configure_otel()` 处理 OTLPSpanExporter 后才能启用**。绝不只修改环境变量就声称当前实现已经支持网络导出。

## 9. 风险、决定与回滚

| 风险 | 处理策略 |
|---|---|
| Kaggle 进程与 Notebook 不是同一个 Python 进程 | 在真正 Worker 入口初始化 SDK；环境变量透传 |
| 多 Worker 的 Trace 断裂 | 第一版统一 `aob.run_id`；后续传播 `traceparent` |
| Console 输出过多 | 仅 Demo 环境开启、限制 Span 数量、后续采样 |
| 用户数据/Secret 泄漏 | 只采元数据，错误事件脱敏；不记录请求体 |
| OTel 导致任务延迟 | 开关关闭回归比对，控制批量导出开销 |
| 与现有 Reporter Schema 冲突 | **不改**既有 HTTPS Event Schema |
| Kaggle Session 中断 | 不保证末尾未导出的 Batch Span；正常终止调用 `shutdown()` |
| 已存在全局 OTel Provider | 初始化前识别现有 Instrumentation，避免二次配置 |

**回滚**：将 Kaggle `AOB_OTEL_ENABLED=false`，重启实际 Worker 子进程，重新执行现有 Demo；任何时候不需要停用现有 Event Ingress 或修改 Oracle BFF。

## 10. 给 Copilot 的实施指令（可直接复制）

> 仅为当前 AOB 项目中运行在 Kaggle 的 Agent/Worker Demo 加入 OpenTelemetry Phase 3.0A。仓库中已无 DDH/DOAP，请勿创建或引用 DDH/DOAP。先搜索真实 Notebook Step 6 启动路径、Worker 入口、Qwen HTTP 推理入口、现有 `run_id` 和 HTTPS Event Reporter。保留现有业务逻辑、`.env` 读取、扩缩容行为和事件 schema；默认关闭 OTel，打开时只输出 Console Trace。使用现有 `run_id` 作为 `aob.run_id` Span 属性，给真实 Worker Task 和 LLM 推理添加最小手动 Span。多进程无上下文传播时不要虚构父子关系。添加安全脱敏、单元测试、回归测试及关闭开关。不要部署 OTLP Collector，不要改 Oracle/BFF/React，不要接入 Azure Application Insights。提交前说明改了哪些具体文件、如何在 Kaggle 验证以及回滚方式。

## 11. 官方参考资料

- [OpenTelemetry Python — Manual instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [OpenTelemetry Python — Exporters](https://opentelemetry.io/docs/languages/python/exporters/)
- [OTLP Exporter Configuration](https://opentelemetry.io/docs/languages/sdk-configuration/otlp-exporter/)
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)

---

**实施边界确认**：当前只做 Kaggle Console Trace 验证；不需要 DDH/DOAP；不影响既有 AOB HTTPS Event Reporting 和 React Live Demo。Azure Application Insights 只读 Connector 可在第二种数据源验证时单独推进。
