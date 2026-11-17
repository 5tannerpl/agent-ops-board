# AOB Phase 1 — API 精简与迁移清单（来自 ZIP 中实际路由）


> **部署基线 v1.1（2026-11-17）**：明确使用 **Oracle Cloud Infrastructure (OCI) Always Free / Ampere A1 ARM64 / Ubuntu Linux / Docker Compose / 单台公网 VM**；GPU 推理与 Autoscaling 继续留在 **Kaggle**。此设计是拟实施方案，尚未对应用源码完成修改。当前 React+BFF 不在 Phase 1 范围内，先使用 Auth0 保护的 Streamlit 过渡 UI。平台资源按 Oracle 官方当期文档与控制台实际资格核实。详见 `06_OCI_ALWAYS_FREE_DOCKER_DEPLOYMENT.md`。


## 判定标准

- **保留（Private）**：仅在私有控制平面挂载，JWT + 白名单 + Run owner 校验；返回净化 DTO。
- **保留（Ingress）**：只有公网 Event Ingress 可以调用，HMAC + Schema + Run 生命周期校验。
- **禁用（Production）**：不注册路由（404），不是仅从前端隐藏按钮。旧实现可保留在显式 Dev App 供本地调试。
- **合并/移除**：减少无需对外提供的独立端点，保留必要后端内部计算函数。

## A. 旧 `/v1/*` 路由（`api/main.py`）

| Method | 原路径 | 当前用途 | Phase 1 决策 | 依据 |
|---|---|---|---|---|
| GET | `/v1/dashboard` | 旧 Streamlit 运营概览 | **生产禁用** | 包含内部模型 URL、PID、DB 路径；Live Demo 有独立状态接口 |
| GET | `/v1/workers` | 全局 Worker 清单 | **生产禁用** | Live Demo 使用 Run-scoped Metrics；防止横向枚举 |
| GET | `/v1/workers/{worker_id}` | Worker 详情 | **生产禁用** | 同上 |
| GET | `/v1/workers/{worker_id}/events` | 原始 Worker 日志 | **生产禁用** | 原始事件内容可能敏感 |
| GET | `/v1/intake-units` | 全局 Unit 搜索 | **生产禁用** | 不属于最小 GPU Demo |
| GET | `/v1/intake-units/{unit_id}` | Unit Detail | **生产禁用** | 不属于最小 GPU Demo |
| GET | `/v1/intake-units/{unit_id}/steps` | Unit Steps/Trace | **生产禁用** | 暂不向公网公开 Agent Trace |
| GET | `/v1/intake-units/{unit_id}/handoff` | Handoff | **生产禁用** | 同上 |
| GET | `/v1/intake-units/{unit_id}/attachments/{attachment_id}` | 附件下载 | **生产禁用** | 当前无授权；如未来恢复需 Unit owner/RBAC + 资源 ACL |
| POST | `/v1/load-tests` | 任意配置的 load/fault test | **生产禁用** | 高计算资源滥用风险；统一从 Demo Start 受控触发 |
| GET | `/v1/load-tests` | 旧运行清单 | **生产禁用** | 用用户自己的 Demo Runs 代替 |
| GET | `/v1/load-tests/{run_id}` | 旧 Load Test 详情 | **生产禁用** | 用 Demo Status/Metrics 代替 |
| POST | `/v1/load-tests/{run_id}/stop` | 停止 Load Test | **生产禁用** | 阶段内不对访客开放终止控制 |
| POST | `/v1/messages` | 提交测试消息 | **生产禁用** | 不属于核心 Demo；不得允许任意消息触发 Worker |

> **注意**：“禁用”仅代表生产 HTTP 路由消失。`intake/service.py`、`loadtest/runner.py`、`workers/*`、`scaling/*` 仍为内部执行组件，**不能**因为删除 API 就删除执行引擎代码。需要同步修改旧 Streamlit 页面调用，否则会出现预期内 404；Phase 1 production UI 只显示 Live Demo。

## B. 当前 `/api/*` Live Demo 路由（`demo_api.py`）

| Method | 原路径 | Phase 1 决策 | 变更 |
|---|---|---|---|
| GET | `/api/me` | **保留 Private** | 返回最小 user id/capabilities，不返回 Auth0 Token |
| POST | `/api/demo/start` | **保留 Private** | 固定 light/high_concurrency 模式，强制 server-side quota 和全局并发 |
| GET | `/api/demo/runs` | **保留 Private** | 只返回 owned Run 的精简 DTO、分页/数量上限 |
| GET | `/api/demo/{run_id}/status` | **保留 Private** | 返回运行阶段、结果、错误代码；与 summary 轻量信息合并 |
| GET | `/api/demo/{run_id}/state` | **保留 Private** | 返回 allowlisted GPU/worker/timeline/summary（不得透传 raw snapshot） |
| GET | `/api/demo/{run_id}/metrics` | **保留 Private** | 返回经净化的 time-series points，最大 200/500 条 |
| GET | `/api/demo/{run_id}/summary` | **移除独立路由** | 当前 UI 客户端不调用；所需 summary 在 `state`/`status` DTO 中已有，内计算函数保留 |
| GET | `/api/demo/{run_id}/events` | **替换为精简验证摘要** | UI 当前需要 `VALIDATION_EVENTS`：迁移为 `GET /api/demo/{run_id}/validation`，只返回固定事件摘要；Raw Events 路由生产禁用 |
| POST | `/api/events` | **移至 Public Ingress ONLY** | HMAC + event allowlist + DB 事务级 Active/expires/key-version + 幂等 |

**兼容策略**：先将 `dashboard/live_demo.py` 的 Event 表改读 `validation`，再关闭 Raw Events；确认使用者后关闭 Summary 独立路由。新 React 直接调用未来 BFF，不依赖旧路径。若消费者确需旧响应，应通过过渡私网兼容层短期提供，不能为了兼容在公网恢复原始路由。

## C. Phase 1 最小 API 合同

### 私有 Control Plane（Auth0 JWT）

```text
GET  /api/me
POST /api/demo/start
GET  /api/demo/runs
GET  /api/demo/{run_id}/status
GET  /api/demo/{run_id}/state
GET  /api/demo/{run_id}/metrics
GET  /api/demo/{run_id}/validation
```

统一 `run_id` 校验、owner 404、结果 DTO、错误代码，不返回 Notebook Ref、Repo URL、PID、DB Path、Event Secret、完整异常。

### 公网 Event Ingress（仅 Kaggle 调用）

```text
POST /api/events
```

不挂载其它路由。`/healthz` 仅内部探针（如通过公网需极简），不启用公开 `/docs`、`/redoc`、`/openapi.json`。可在边缘进一步按 Method/Content-Type/Size 限制，但不能代替应用内校验。

## D. 浏览器可展示的数据结构示例（示意，不是已实现接口）

```json
{
  "run_id": "run-<opaque-id>",
  "status": "running",
  "mode": "high_concurrency",
  "units": 150,
  "result": "pending",
  "phase": "validating",
  "error_code": null
}
```

`LiveState` 仅包含 Run 级状态（scaling_state, live, telemetry_delayed）、2 个 GPU 的**状态与完成计数**、Worker 数量、允许字段的 Timeline、Summary；MetricsPoint 包含时间戳、pending/running/completed/failed、worker_count、gpu_instance_count、吞吐/延迟等数值。**必须根据真实事件字段做 schema 映射，禁止直接 `return event['payload']`**。

## E. 使用关系核对

- `dashboard/api_client.py` 仍定义全套 `/v1/*` 客户端；当生产删路由后需要从 production UI 入口断开旧 overview/intake/worker 页面。
- `dashboard/live_demo.py` 读 `/api/demo/start`、Runs、Status、State、Metrics、Raw Events；迁移 Raw Events 到 Validation DTO。
- `demo_scaling.py` 的 `state()`/`summary()` 当前包含原始 payload，改使用 DTO 映射。
- 测试 `tests/test_api_and_loadtest.py` 对旧 `/v1` 的断言应转到 **dev legacy suite**；新 production 测试应断言 `/v1/*` 不存在。


## F. OCI Reverse Proxy 路由白名单（v1.1）

- `dashboard.example.com/*` → Streamlit，仅 HTTPS；认证必须 Fail Closed。Streamlit WebSocket 通过正常反向代理转发，但该域名**不**路由到 FastAPI。
- `events.example.com/api/events` → Event Ingress，**仅 POST**、大小/速率限制、HMAC/重放验证。其他路径和方法均 404/405，不代理到 `control-api`。
- `control-api` → Docker 私有 DNS 名称，仅由 Dashboard 服务端调用，绝不配置宿主 `ports:`。`/api/demo/*` 私有 API 可用但不可直接从互联网访问。
- 生产不启动 `api.main:app`，不注册 `/v1/*`，不给旧 Load Test/Intake/Attachment API 配置任何公网代理规则。
- 原 `GET /api/demo/{run_id}/events` 和 `GET /api/demo/{run_id}/summary` 的替换/移除应和 `dashboard/live_demo.py` 调用改造一起提交；不能仅删 API 导致界面回归。
