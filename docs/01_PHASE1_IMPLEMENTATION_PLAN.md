# AOB Phase 1 — Security Hardening 实施技术方案


> **部署基线 v1.1（2026-11-17）**：明确使用 **Oracle Cloud Infrastructure (OCI) Always Free / Ampere A1 ARM64 / Ubuntu Linux / Docker Compose / 单台公网 VM**；GPU 推理与 Autoscaling 继续留在 **Kaggle**。此设计是拟实施方案，尚未对应用源码完成修改。当前 React+BFF 不在 Phase 1 范围内，先使用 Auth0 保护的 Streamlit 过渡 UI。平台资源按 Oracle 官方当期文档与控制台实际资格核实。详见 `06_OCI_ALWAYS_FREE_DOCKER_DEPLOYMENT.md`。


**版本**：1.1 · **基线**：用户上传 `agent-ops-board.zip`（2026-11-17）· **状态**：设计方案，尚未实施

## 1. 目标与边界

Phase 1 的目标是把当前 AOB 从“Streamlit 与 FastAPI 混合的实验性环境”收口为可以作为 React + FastAPI + BFF 目标架构基础的**安全后端**。**不**在 Phase 1 重写 React、不更换 GPU 执行引擎、不重构 Autoscaler，不承诺修改代码已完成。

**目标架构原则**：公开浏览器只能访问 HTTPS UI/BFF；Kaggle 只向专用 Event Ingress 发送 HMAC 事件；控制平面 FastAPI、数据库和 Kaggle Launcher 均为私有；授权与过滤在服务端执行；浏览器永不接触 Kaggle/GitHub/Event Master Secrets。Phase 1 期间 Streamlit 作为过渡 UI，仅向已登录用户开放；BFF Session Cookie 在 Phase 3 引入，**不能把尚未存在的 BFF 当成 Phase 1 安全控制**。

**上线前阻断条件**：
1. 所有公网入口由反向代理/边缘策略显式 allowlist；不能把整个 `127.0.0.1:8000` 通过 Quick Tunnel 公开。
2. `/v1/*` 要么在生产根本不挂载，要么只在私网、经明确机器身份与 RBAC 保护；不得依靠“前端不显示按钮”。
3. 旧 HMAC 主密钥更换，历史 Notebook 含密钥版本被删除/限制访问；终态 Run 拒收事件。
4. 出站到 Kaggle 的凭证不得写入前端/日志/发布包；event payload 与异常必须脱敏。
5. CI 检测成功，发布物不含 `venv/`、`runtime/`、`.db`、真实 Notebook 或密钥。

## 2. 当前代码的真实构成

- `api/main.py` 当前把 `demo_api.router` 和 **全部 `/v1/*`** 挂入同一个 FastAPI 实例（第 33–34 行），其中 `/v1/*` 没有 Auth0 依赖。
- `demo_api.py` 当前 `GET /api/me`、`POST /api/demo/start`、Runs/Status/State/Metrics/Summary/Events 使用 `require_demo_access` 和 Run owner 校验；`POST /api/events` 用签名验证。
- `dashboard/app.py` 的旧概览、Worker/Intake 页面使用 `/v1/*`；`dashboard/live_demo.py` 使用 `/api/demo/*`。
- `dashboard/auth_gate.py` 没有配置时可能关闭登录；`dashboard/app.py` 原有 Dashboard 路由需要额外隔离。`kaggle_launcher.py` 每次把 per-run Key 写入私有 Notebook 代码单元；Notebook 还包含 Git clone 流程。
- 项目 ZIP 含两个运行 SQLite、附件、历史输出以及大量 `venv/`；另有 `tests/test_live_demo_panel.py` 语法错误。审计报告详见项目内此前生成的 security audit。

## 3. Phase 1 对外提供的最小产品能力

**仅 Private Live GPU Demo**：Auth0 邀请用户登录 → 启动 light（固定 10 units）或 high_concurrency（50/100/150 units）→ 查看自己的 Runs、Status、实时 Worker/GPU Metrics、验证结果和脱敏事件摘要。**保留 Qwen、Kaggle、Scaling 的既有流程**。

**本期不对外提供**：手动消息提交、通用 Intake CRUD、附件下载、任意 Worker 查询、在网页触发旧 `/v1/load-tests`、公开的原始事件/错误明细、服务器进程/PID/模型 URL。旧 Dashboard 的相应功能在 production UI 禁止渲染；仅保留本地内部调试能力（由显式 Dev Mode 控制）。

## 4. 实施工作包（顺序必须遵守）

### P1-01 立即隔离公网 API（阻断 P0）
- 新建**独立 ASGI 入口**：`api/public_ingress.py` 只挂载 `POST /api/events` 与健康探针；`api/private_app.py` 挂载私有 `/api/demo/*`，不包含公共 Webhook 路由；不要继续在生产调用当前完整 `api.main:app`。
- 为避免本地功能退化，保留 `api/main.py` 作为 `dev_legacy_app`，但**生产环境拒绝启动**（`AOB_ENV=production` 时触发 RuntimeError）。更理想是拆 `api/legacy_routes.py`，仅本地显式 `AOB_ENABLE_LEGACY_V1=1` 时挂载。
- 反向代理：公网 HTTPS `POST /api/events` → ingress，仅开放 UI 所需静态/认证流；`/v1/*`、`/api/demo/*`、`/docs`、`/openapi.json` 在 Event Ingress 都返回 404（不要只靠 Nginx hide）。
- `public_ingress.py` 不暴露 demo 路由，不携带 Kaggle CLI 控制能力。对 Ingress 独立限速、体积限制、超时和无效签名统计。
- **过渡限制**：原 Streamlit 向私网控制平面调用，若部署为另一主机，必须有受保护私网/VPN/内部认证连接；不得通过公网无保护反代 API。

### P1-02 认证授权统一 Fail-Closed
- `AOB_ENV=production` 强制检查 `AUTH0_DOMAIN`、`AUTH0_AUDIENCE`、Allowed Users 文件、`EVENT_SECRET`、HTTPS Events URL；缺失直接启动失败。
- `dashboard/auth_gate.py` 在 production 必须登录；把旧 Streamlit Dashboard（`dashboard/app.py` 中 `/v1` 显示）迁移为**仅 Private Live Demo 页面**，生产禁止调用旧 API。
- `demo_auth.py` 继续校验 RS256、iss、aud、exp 与白名单；`owned_run` 保留 owner 404 模式。新 API 统一使用安全响应 DTO。
- 不让 React/Streamlit 自行决定授权。未来 BFF 处理 Token，Phase 1 暂由 Streamlit 服务端持有用户 Access Token 调私有 Demo API。

### P1-03 Webhook 密钥与生命周期
- **立即 rotate `AOB_EVENT_SECRET`**，并按泄漏凭证事件排查 Git/Kaggle 历史；轮换会使在途 Run 原有签名失效，应安排维护窗口、终止旧 Run 并声明旧事件不能恢复。
- 改 `demo_store.add_event()`：用**单个数据库事务**校验 Run 为 `submitting|queued|running`、有效 `expires_at`（新增字段）及当前密钥版本，插入事件并更新状态；对终态/过期 Run 403 或 410，重复 `event_id` 应幂等。
- `demo_api.receive_event()` 限制事件类型、payload Schema、字段长度/嵌套；先验证 HMAC，再解析精简结构，最终在 DB 事务中验证 Run 活跃和密钥状态；签名不合法不透露 Run 是否存在。
- per-run key 当前由主密钥派生，只具备 Run 隔离，不自动过期；新增过期/撤销语义。建议密钥从 Secret Manager 读取；不应将 Key 明文写入发布物。**现有动态 Notebook 注入 per-run key 暂作为 Phase 1 已知风险**，必须设 Private 并确认 Kaggle 历史保留范围；Phase 2+ 优先改用只读 Kaggle Secret/短期凭证引导方案。
- 若对已结束 Run 仍有末尾指标上报需求，先协议化“final event 在标记 complete 前入库”；禁止用无限终态写入作为补偿。

### P1-04 数据最小化与错误净化
- 所有 UI/API 返回用白名单 DTO，不得原样回传 `event.payload`、`run.error`、`snapshot` 中未知字段。
- 为展示需求定义 `RunListItem`、`RunStatus`、`LiveState`、`MetricsPoint`、`ValidationEventSummary` 的模型，并设置 `extra='forbid'` 于入站数据模型。
- 对外返回稳定 `error_code`（如 `KAGGLE_SUBMISSION_FAILED`）及 `request_id`；真实 stderr、异常堆栈写入有访问控制且脱敏的服务端日志。
- 不向 UI 输出 `MODEL_URL`、`db_path`、PID、Notebook Ref、Git URL、Tunnel 域名、Auth Token。

### P1-05 依赖、产物和 CI
- 发布构建采用精确 allowlist，不从整个工作目录压 ZIP。排除 `.git/`, `.venv/`, `venv/`, `runtime/`, `*.db`, `.env*`, `secrets.toml`, `allowed_users.json`, `notebook-check/`, Notebook 执行输出。
- 运行 Secret Scan（工作区 + Git 历史），检查依赖版本与漏洞，锁定依赖和 Kaggle 执行代码的 commit SHA；**单纯固定 branch 不够**。
- 修复 `tests/test_live_demo_panel.py` 的 SyntaxError；新增 API AuthZ、event 终态拒收、脱敏、发布物检查等测试。

## 5. 部署约束与最小配置

| 配置 | 生产要求 |
|---|---|
| `AOB_ENV` | `production`，不可自动降级开发模式 |
| `AOB_AUTH0_DOMAIN`, `AOB_AUTH0_AUDIENCE` | 非空，严格校验 issuer/audience |
| `AOB_ALLOWED_USERS_FILE` | 仅后端可读、缺失全部拒绝 |
| `AOB_EVENT_SECRET` | 轮换后随机高熵 Secret，权限 0600/Secret Store |
| `AOB_EVENTS_URL` | 稳定自有 HTTPS 域名，只指向 dedicated ingress |
| `AOB_KAGGLE_NOTEBOOK_REF` | 后端 Secret/config，私有 Notebook |
| `AOB_REPO_URL`, `AOB_REPO_COMMIT` | 受控仓库与已审查 commit SHA；禁无固定分支执行 |
| `AOB_DB_PATH`, `AOB_DEMO_DB_PATH` | 主机持久化目录，权限只给服务账号 |
| `AOB_ENABLE_LEGACY_V1` | production 固定为 `0`，不是安全豁免开关 |

**部署拓扑**（过渡期）：公网 `dashboard.example.com` → Streamlit（必须 Auth0）；公网 `events.example.com` → 仅 Event Ingress；私网 `control-api:8000` → Private FastAPI Demo API；Private API → SQLite（单机）/Kaggle CLI；Kaggle → `events.example.com/api/events`。将来 React/BFF 替代 Streamlit 入口，而不改变 Private API / Event Ingress 边界。

## 6. 交付顺序、验收与回滚

交付 PR 分批：PR1 分离 ASGI Entrypoint + Network ACL；PR2 JWT/生产配置/旧页面收口；PR3 Event Key 轮换/终态原子拒收；PR4 DTO/Schema/日志；PR5 CI/锁版本/干净部署。

**必须通过**：
- 未登录用户无法启动/查看任何 Live Run；白名单外账户 403；User A 查询 User B Run 404。
- Event Ingress 对 `GET /api/demo/runs`、`GET /v1/dashboard`、`POST /v1/load-tests`、`GET /docs` 返回 404（或边缘直接拒绝）；仅签名正确且未过期的 Active Run 可写事件。
- 旧 Key/终态 Run/未知 event type/过大 Payload/重放数据无法污染结果；Event ID 重复幂等。
- React 将来所需所有 UI 指标均能从精简响应取得；没有 Notebook URL、GitHub Repo、密钥、内部目录、原始异常或附件数据。
- GPU Demo 仍能完成 Light 和 High Concurrency，2→8→2 Worker / 1→2→1 GPU，展示真实上报数据而非伪造值。
- CI 测试及产物 Secret 检查通过；操作日志可审计而不包含 Secret。

**回滚**：保留旧版本仅限私网；撤销公网路由不得回滚；已轮换 Secret 不回滚到旧值。需回滚应用时兼容新 DB Schema，使用备份做恢复演练。旧 Run 的签名应按旧密钥撤销处理，避免“双密钥同时开放”。

## 7. 明确不做（防止 Phase 1 膨胀）

不开发 React、不引入 BFF Session Cookie、不迁移 PostgreSQL、不重写 Autoscaler/Queue、不引入 Kubernetes、不提供客户多租户隔离。后两项属于商业 SaaS 阶段；当前白名单用户按 owner_sub 隔离 Demo Run。


## 8. OCI Always Free / Docker 实施补充（v1.1）

- 目标实例：OCI home region 中标有 Always Free Eligible 的 `VM.Standard.A1.Flex`，建议一台 Ubuntu ARM64 VM，按可用配额申请合计 **最多 2 OCPU / 12 GB RAM**。**不能假定一定能获得容量**；创建时检查容量和账户费用状态。
- 单 VM 多容器：`reverse-proxy`、`dashboard`、`control-api`、`event-ingress`；Kaggle GPU 在 Oracle 云外部。反向代理仅映射宿主机 `80/443`；`control-api` 不使用 `ports:`，Ingress 也只由反向代理访问。
- Oracle VCN NSG/Security List 和宿主机防火墙同时限制：443 公网、80 ACME/HTTPS 跳转、22 管理 IP；禁用公网 8000/8010/8011/8501/5432。
- 镜像必须是 `linux/arm64`：镜像构建和 Python 依赖在 CI 或 VM 上做平台一致性检查；不要复制 Windows 或 amd64 venv。
- 使用 Docker named volume / 受权限保护的宿主目录持久化 SQLite；SQLite 单写者交易、锁竞争、数据库备份一致性需验证。禁止将历史 `runtime/` 直接 COPY 入镜像。
- Docker Secret 使用 root 可读的主机文件（Compose `secrets:`），并明确应用代码需从 `/run/secrets/...` 读取；不把 Secret 直接写入 Compose/YAML、容器环境变量或镜像层。初期无付费 Secrets 服务依赖。
- 建立 CPU/内存/PID/日志大小限制、健康检查和磁盘空间告警；单机资源故障是 MVP 已知风险。
- 脚本操作和容器部署说明见第 06/07 文件；不自动把示例部署定义为可直接上线。
