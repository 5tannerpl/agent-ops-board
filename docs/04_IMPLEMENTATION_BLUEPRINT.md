# AOB Phase 1 — 代码实施蓝图、Schema 与测试


> **部署基线 v1.1（2026-11-17）**：明确使用 **Oracle Cloud Infrastructure (OCI) Always Free / Ampere A1 ARM64 / Ubuntu Linux / Docker Compose / 单台公网 VM**；GPU 推理与 Autoscaling 继续留在 **Kaggle**。此设计是拟实施方案，尚未对应用源码完成修改。当前 React+BFF 不在 Phase 1 范围内，先使用 Auth0 保护的 Streamlit 过渡 UI。平台资源按 Oracle 官方当期文档与控制台实际资格核实。详见 `06_OCI_ALWAYS_FREE_DOCKER_DEPLOYMENT.md`。


> 此文件是实施设计和代码审查清单，**不是已经完成的代码补丁**。必须由开发者在独立 PR 中应用并测试。

## 1. 建议目录（最小改动）

```text
agent-ops-board/
├── api/
│   ├── private_app.py             # 新：只注册私有 Demo Router；生产身份依赖
│   ├── public_ingress.py          # 新：只注册 Event Ingress，docs_url=None
│   ├── legacy_app.py              # 新/迁移：旧 /v1，仅 local dev 启用
│   ├── response_models.py         # 新：显式白名单响应 DTO
│   └── main.py                    # 迁移兼容；生产禁止直接使用完整旧 app
├── demo_api.py                   # 拆 private demo router 与 ingress handler
├── demo_auth.py                  # 保留 JWT/HMAC，扩展 event key 版本/生命周期
├── demo_store.py                 # 新增终态拒收、Key Version、expiry 的原子性
├── demo_scaling.py               # 净化 state/metrics/summary
├── demo_service.py               # 统一错误代码、运行生命周期
├── kaggle_launcher.py            # 固定 SHA、日志清理、轮换流程
├── dashboard/
│   ├── app.py                    # Phase 1 生产只渲染 Private Demo
│   ├── auth_gate.py              # production fail-closed
│   ├── api_client.py             # 不调用旧 /v1，改用 validation DTO
│   └── live_demo.py              # 不显示 raw payload
├── security/
│   ├── event_schemas.py          # 新：事件 allowlist 和 Payload Schema
│   ├── redaction.py              # 新：日志与响应脱敏
│   └── production_config.py      # 新：启动前配置校验
├── deploy/
│   ├── nginx-events.conf         # Public Ingress only
│   ├── services.md               # 两个不同端口/安全组
│   └── release-check.sh          # 发布包安全校验
└── tests/
    ├── test_security_routes.py
    ├── test_security_authz.py
    ├── test_security_events.py
    └── test_security_redaction.py
```

## 2. 独立 ASGI app 示例骨架

```python
# api/public_ingress.py — illustrative
from fastapi import FastAPI
from demo_api import ingress_router  # 从现有 demo_api 中拆出，仅包含 POST /api/events

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(ingress_router)
```

```python
# api/private_app.py — illustrative
from fastapi import FastAPI
from demo_api import private_router  # Demo 用户接口，不包含 POST /api/events

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(private_router)
```

**重要**：仅创建两个文件**不够**。需要把现有 `demo_api.router` 真正拆成两个路由，否则 `include_router(demo_api.router)` 会重新暴露所有端点。旧 `api/main.py` 还需从生产启动命令中排除。不能在 FastAPI `include_router()` 之后用前端/代理规则伪装“路由已经删除”。

## 3. 事件 Schema 与数据库交易模型

### 建议新增 DB 字段（用 migration，不要直接清空旧 DB）

```sql
ALTER TABLE demo_runs ADD COLUMN event_key_version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE demo_runs ADD COLUMN event_expires_at REAL;
ALTER TABLE demo_runs ADD COLUMN event_revoked_at REAL;
```

迁移时将**所有历史 Run**的 `event_revoked_at` 置为迁移时间，尤其是当前终态 Run；新 Run 创建时生成 `event_expires_at = now + max_run_seconds + bounded_finalization_grace`，并在启动前确认在运行生命周期内可验证。

**事件接收算法（伪代码）**：

```text
POST /api/events
  reject if content-length exceeds cap / streaming body exceeds cap
  reject if timestamp invalid, HMAC invalid, content-type wrong
  parse event -> strict per-type schema -> whitelist payload
  BEGIN IMMEDIATE TRANSACTION
    SELECT run FOR UPDATE equivalent (SQLite: BEGIN IMMEDIATE)
    reject if unknown, terminal, expired, revoked, or key version mismatch
    if (run_id,event_id) exists -> idempotent duplicate
    else INSERT allowlisted event
    update known progress fields as needed
  COMMIT
  return {status: "accepted"|"duplicate"}
```

必须以数据库交易完成终态与插入的竞争控制，不能先 `get_run()`、再独立 `add_event()`。当前 `demo_store.py` 的 `INSERT OR IGNORE` 只实现重复 Event ID 幂等，不实现终态隔离。

**签名与版本**：当前 `run_event_key(run_id)` 使用 `AOB_EVENT_SECRET` 派生 HMAC。新实现应将 `event_key_version` 绑定到派生 context（例如 `run:{run_id}:v:{version}`），同时实现服务端旧版本拒绝，不在 `event` JSON 中接收任意可选版本绕过校验。Secret 轮换后从安全配置加载新密钥，旧密钥不再认可；运行中迁移需要维护窗口。

**Event Schema**：按代码实际存在的 `notebook_started`、`environment_ready`、`model_located`、`model_ready`、`model_validation_started`、`worker_validation_completed`、`modelcheck_completed`、`model_validation_completed` 和 `scaling_*` 事件枚举；保留当前使用的 scalers snapshot/completed/failed/timeline 必须字段。先从 Notebook 真实 Event 样例生成模型/fixture，再限制额外字段。严禁直接使用 `payload: dict` 作为生产公开数据模型。

## 4. API DTO 映射原则

当前返回链：`demo_api.run_view` 返回原始 `error`，`demo_scaling.state` 和 `summary` 返回 raw payload，`dashboard/live_demo._events_table` 逐项展示 payload。

目标：
- `run_view()` -> `RunStatusResponse` 明确字段，内部 `error` 只转化为标准 `error_code`。
- `demo_scaling.state()` -> `LiveStateResponse`，其中 snapshot/timeline/failure/summary 必须字段映射，默认丢弃未知字段。
- `validation()` -> EventSummary list，只返回时间、event_type、normalized status、safe numbers（模型是否 ready、validation passed 等）；不返回完整 `payload`。
- 限制 `GET /runs` 返回数量，Event Ingress event max per Run/size 保持，必要时设置每单位时间阈值。

## 5. 删除旧 API 的实现顺序（避免自我破坏）

1. 从 `api/main.py` 提取/复制旧路由到 `api/legacy_app.py`；设置仅 dev 可加载，明确生产启动参数为新 `private_app` 与 `public_ingress`。
2. 私有 Demo Router、Ingress Router 分离；两者拥有不同进程运行身份、不同暴露方式。配置独立代理，测试 `/v1` 和 Docs 不可达。
3. 从 production Streamlit 页面删旧 Overview/Workers/Intake/LoadTest 控件及调用（旧页面源文件可以先保留，但生产入口绝不挂载）。
4. 保留 `/api/demo` 最少路由；新增 Validation DTO 后，删除 Raw Events 和独立 Summary 路由。
5. 建立 production URL allowlist 断言测试；扫描 OpenAPI/route table 确认只有名单内的端点。
6. 等 React/BFF 进入下一阶段，再决定 `/api/me` 的对外 façade（BFF 会话信息）与 Private API 的访问模型，不提前暴露新的 Browser API。

## 6. 基础测试矩阵

| Test ID | 条件 | 期望 |
|---|---|---|
| SEC-R01 | 扫描 Public Ingress route table | 仅 POST /api/events、必要内部 health；不包含 /api/demo、/v1、docs |
| SEC-R02 | Public Ingress GET /api/demo/runs | 404 |
| SEC-R03 | Private Control Plane POST /api/events | 404 |
| SEC-A01 | `/api/demo/start` 无 JWT | 401 |
| SEC-A02 | JWT 有效但不在 whitelist | 403 |
| SEC-A03 | User A 读 User B Run | 404 |
| SEC-E01 | 正确 HMAC + 活跃 Run + 合法事件 | accepted |
| SEC-E02 | 旧 Key、错误 HMAC 或过期时间戳 | 401，不修改 DB |
| SEC-E03 | Run 已 COMPLETE/FAILED/CANCELLED | 403/410，不修改 DB |
| SEC-E04 | 相同 run_id + event_id | duplicate，不新插入 |
| SEC-E05 | 未知 event_type / 未知 payload 字段 | 422 |
| SEC-E06 | 事件超过大小上限 | 413 |
| SEC-O01 | 模拟 Launcher 抛含内部 URL/token 的异常 | API 返回 error_code，无泄漏 |
| SEC-O02 | Snapshot 注入 `secret`, `repo_url`, `model_url` | 所有 Private API DTO 都过滤 |
| SEC-B01 | 构建发布包 | 不含 db、runtime、venv、Notebook 密钥、Secrets |
| SEC-C01 | 编译所有源码/pytest | 通过，修复已知测试语法错误 |
| SEC-L01 | Light Demo | 真实 10 units 完成并可展示 |
| SEC-L02 | High 150 units | 真实 Worker 2→8→2 / GPU 1→2→1，事件不丢失 |

**注意状态竞态**：Kaggle 的 notebook final event 与 CLI COMPLETE 可能交错。先写竞争测试，再决定有限 finalizing state 的处理顺序，不能为了通过测试而重新接受终态事件。

## 7. 上线验收证据

PR 附：路由表、无 Token 访问结果、Auth0 whitelist/owner 测试结果、Event 终态拒收数据库校验、Secret Scan 输出（仅摘要）、发布包文件清单、Light/High GPU 实际执行截图与运行日志（遮蔽敏感信息）。生产上线后执行一次真实 HMAC 端到端探测和拒绝探测，保存审计工单。


## 8. Docker/ARM64 代码改造清单（v1.1）

- 新建 `deploy/oci/docker-compose.yml`、`deploy/oci/Caddyfile`、`.dockerignore`、`Dockerfile`（或为 dashboard/control/ingress 分开镜像），按 `linux/arm64` 构建并验证所有 wheels。
- 应用启动前校验 `AOB_ENV=production`、Auth0 配置、Secret 文件存在性、持久化路径权限、允许用户配置；`api.main` 的旧路由不能在生产启动。
- 给 `public_ingress` 与 `private_app` 分开模块装配/依赖；Ingress 不应意外继承 Kaggle 启动权限。
- 将 `AOB_EVENT_SECRET` / Auth0 Client Secret / Kaggle CLI 凭证移到单独只读 Secret mount，并新增 `_FILE` 风格环境变量兼容代码或文件读取函数；现有读取 `os.getenv` 的代码不会自动读取 Docker Secret 文件。
- `.dockerignore`/CI 必须排除 `venv`, `.venv`, `.git`, `.env*`, `.streamlit/secrets.toml`, `runtime`, `*.db`, `notebook-check`, `*.ipynb`, 私钥；以容器镜像和发布上下文为对象二次扫描。
- Docker 进程非 root、`read_only` 根文件系统（若依赖临时写入则使用 tmpfs）、cap drop、`no-new-privileges`；明确用于 Kaggle CLI 的持久化路径和当前用户 UID 权限。
- 单机 SQLite 与事件读写：不跨网络共享 SQLite；测试 WAL/锁冲突、多个 FastAPI Worker 进程并发、备份和重启恢复。
- 提供 `docker compose config`、容器健康检查、无公网控制 API、平台架构和 Kaggle 回归测试门禁。
