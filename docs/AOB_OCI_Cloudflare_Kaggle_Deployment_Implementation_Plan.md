# AOB OCI + Cloudflare + Kaggle GPU 部署实施方案

## 1. 文档目标

本文档定义 Agent Ops Board 部署到 Oracle Cloud Infrastructure (OCI) Always Free ARM64 主机，并通过 Cloudflare 使用 `dipperwoven.com` 域名，同时继续使用 Kaggle GPU 执行 Live Demo 的实施方案。

本方案不把 Streamlit 部署到 Cloudflare Pages。Streamlit 作为 OCI 上的长运行服务，由 Cloudflare DNS/Proxy 提供 HTTPS 域名入口。

目标代码基线：

```text
OPENTELE
```

当前 Trace Upload 修复版本：

```text
34505bf3ca3e8406b278e2f9c86d2dfe382b40cc
```

## 2. 目标架构

```text
GitHub
   |
   | GitHub Actions
   | test + linux/arm64 image build + registry push
   v
GHCR 或 OCI Container Registry
   |
   | docker compose pull
   v
OCI Always Free ARM64 VM
   ├── Caddy
   ├── Streamlit Dashboard
   ├── Private FastAPI Control API
   ├── Public Event/Trace Ingress
   └── SQLite WAL + persistent volume
          |
          v
Cloudflare DNS / Proxy
   ├── aob.dipperwoven.com
   └── events.dipperwoven.com
          |
          v
Kaggle GPU Notebook
   ├── llama.cpp / GPU
   ├── workers
   ├── scaling demo
   ├── supervisor.log OTel spans
   └── signed HTTPS event/trace upload
```

职责边界：

| 组件 | 职责 |
|---|---|
| Cloudflare | DNS、HTTPS、Proxy、基础边缘防护 |
| Caddy | OCI 唯一公网反向代理、TLS、Host/Path 路由 |
| Streamlit | Dashboard 和 Live Demo UI |
| Private API | Auth0、Run 生命周期、Kaggle Launcher、Trace 查询 |
| Public Ingress | `/api/events` 和 `/api/traces` |
| SQLite | Run、Event、Trace Batch、Trace Span 持久化 |
| Kaggle | GPU、模型推理、Worker、Scaling 和 OTel 生成 |
| GitHub Actions | 测试、ARM64 镜像构建、发布、部署 |

不引入：

```text
OpenTelemetry Collector
Tempo
Grafana
Jaeger
新的 Observability 服务
```

## 3. 域名规划

推荐两个子域名：

```text
aob.dipperwoven.com
events.dipperwoven.com
```

用途：

```text
https://aob.dipperwoven.com
    -> Caddy -> Streamlit Dashboard

https://events.dipperwoven.com/api/events
    -> Caddy -> Public Event Ingress

https://events.dipperwoven.com/api/traces
    -> Caddy -> Public Trace Ingress
```

Private Control API 不分配公网域名，不直接暴露 `8010`。

## 4. OCI 主机

### 4.1 推荐规格

```text
Instance: VM.Standard.A1.Flex
OS: Ubuntu LTS ARM64
CPU: 2 OCPU（以账户免费配额为准）
Memory: 8–12 GB（以账户免费配额为准）
Disk: 约 50 GB 系统盘
IP: Reserved Public IP
```

OCI 只运行控制面和持久化层，不运行 GPU 模型推理。

### 4.2 主机软件

```text
Docker Engine
Docker Compose Plugin
Caddy
系统安全更新
日志轮转
基础磁盘/内存监控
```

不安装：

```text
GPU 驱动
Kaggle Notebook 运行环境
OTel Collector
Grafana
Tempo
Jaeger
```

### 4.3 OCI 网络规则

仅允许：

```text
22/tcp   管理员固定公网 IP /32
80/tcp   HTTP/ACME
443/tcp  HTTPS
```

禁止公网访问：

```text
8010  Private API
8011  Public Ingress
8501  Streamlit
8000  旧 API
SQLite
Docker API
```

容器后端端口只加入 Docker private network，不通过宿主机 `ports` 直接发布。

## 5. Cloudflare 配置

### 5.1 DNS

添加：

```text
aob       A       OCI_RESERVED_PUBLIC_IP
events    A       OCI_RESERVED_PUBLIC_IP
```

建议启用 Cloudflare Proxy。

### 5.2 TLS

```text
Cloudflare SSL/TLS: Full (strict)
```

Caddy 使用以下任一方式取得源站证书：

```text
Caddy ACME
Cloudflare DNS Challenge
Cloudflare Origin Certificate
```

禁止使用：

```text
Flexible SSL
```

### 5.3 WebSocket

上线前验证：

- Cloudflare WebSocket 可用。
- Caddy 正确转发 Upgrade/Connection。
- Streamlit Live Refresh 正常。
- 长连接不会被空闲超时过早关闭。

## 6. 容器组件

### 6.1 Caddy

职责：

- Dashboard Host 路由。
- Event Ingress Host 路由。
- HTTPS。
- Method/Path 白名单。
- 请求日志和基础限制。

### 6.2 Dashboard

运行：

```text
dashboard/app.py
```

内部 API 地址：

```text
AOB_API_URL=http://control-api:8010
```

Dashboard 不持有：

```text
Kaggle credentials
AOB_EVENT_SECRET
```

### 6.3 Private Control API

运行：

```text
api.private_app:app
```

职责：

- Auth0 身份和用户白名单。
- Run 创建、选择和状态查询。
- Kaggle Launcher。
- Trace Summary。
- LLM Calls。
- Unit Trace。

该服务持有 Kaggle CLI 和相关凭据。

### 6.4 Public Ingress

运行：

```text
api.public_ingress:app
```

公网允许：

```text
POST /api/events
POST /api/traces
GET /healthz
```

Public Ingress 不持有 Kaggle 凭据和 Auth0 管理权限。

## 7. 持久化和数据目录

建议宿主机目录：

```text
/srv/aob/data
/srv/aob/backups
/srv/aob/secrets
```

数据库：

```text
demo.db
intake.db
```

建议启用：

```text
SQLite WAL
一致性备份
在线备份 API
日志轮转
磁盘空间告警
```

不要在写入期间直接复制 SQLite 文件。

### Trace retention 限制

当前实现会在 Run 接收窗口结束后拒绝新的 Trace，但不会自动删除旧的：

```text
trace_batches
trace_spans
```

上线前需要增加：

- Trace 最大保留天数。
- 定时清理任务。
- 清理审计。
- 清理前备份策略。
- 磁盘容量阈值和告警。

在清理机制完成前，不应将该存储描述为长期无限保留。

## 8. Secret 注入

使用：

```text
Docker Compose secrets
OCI Vault
权限受限宿主机 Secret 文件
```

不得将 Secret 写入：

```text
Git
Dockerfile
镜像层
.env
Notebook
Streamlit 页面
日志
```

主要 Secret 名称：

```text
AOB_EVENT_SECRET_FILE
KAGGLE_USERNAME
KAGGLE_KEY
Auth0 配置
Allowed Users 文件
```

历史 Event Secret 如曾进入 Notebook staging、日志或测试环境，应在生产部署前轮换。

## 9. 环境变量

### 9.1 API、Auth 和存储

```text
AOB_ENV
AOB_API_URL
AOB_DB_PATH
AOB_DEMO_DB_PATH
AOB_ALLOWED_USERS_FILE
AOB_AUTH0_DOMAIN
AOB_AUTH0_AUDIENCE
AOB_EVENT_SECRET_FILE
AOB_EVENTS_URL
AOB_TRACE_URL
AOB_ENABLE_LEGACY_V1
```

建议：

```text
AOB_ENV=production
AOB_ENABLE_LEGACY_V1=0
AOB_EVENTS_URL=https://events.dipperwoven.com/api/events
AOB_TRACE_URL=https://events.dipperwoven.com/api/traces
```

### 9.2 Kaggle Launcher

```text
AOB_KAGGLE_BIN
AOB_KAGGLE_NOTEBOOK_REF
AOB_REPO_URL
AOB_REPO_BRANCH
KAGGLE_USERNAME
KAGGLE_KEY
```

建议：

```text
AOB_REPO_BRANCH=OPENTELE
```

### 9.3 OTel

```text
AOB_OTEL_ENABLED
AOB_OTEL_EXPORTER
AOB_OTEL_SERVICE_NAME
AOB_OTEL_ENVIRONMENT
AOB_OTEL_COMMIT
```

真实验收：

```text
AOB_OTEL_ENABLED=true
AOB_OTEL_EXPORTER=console
AOB_OTEL_COMMIT=<完整 40 字符 Git SHA>
```

## 10. GitHub Actions

### 10.1 Pull Request

每个 Pull Request 执行：

```text
Python syntax/type checks
Focused pytest
Full pytest
Secret scanning
linux/arm64 Docker build validation
```

至少覆盖：

```text
Trace ingestion
Trace correlation
Trace uploader
Dashboard regression
Worker OTel
Scaling events
Auth0/API permissions
```

### 10.2 OPENTELE 发布

推荐流程：

```text
1. Checkout Git SHA
2. 运行测试
3. 构建 linux/arm64 镜像
4. 使用 Git SHA 作为镜像 tag
5. Push 到 GHCR 或 OCI Container Registry
6. SSH 到 OCI
7. docker compose pull
8. docker compose up -d
9. 运行 health checks
10. 运行 smoke tests
11. 输出部署 SHA
```

推荐镜像 tag：

```text
34505bf3ca3e8406b278e2f9c86d2dfe382b40cc
```

不要只使用 `latest` 作为生产版本标识。

### 10.3 镜像平台

必须支持：

```text
linux/arm64
```

可选多架构：

```text
linux/amd64,linux/arm64
```

不要只构建 `linux/amd64` 后部署到 OCI ARM64。

## 11. 部署阶段

### 阶段 1：OCI

交付：

- ARM64 Always Free VM。
- Reserved Public IP。
- SSH 加固。
- Docker。
- NSG。
- 持久化 Volume。
- 系统监控。

验收：

```text
SSH 正常
Docker 正常
80/443 可用
8010/8011/8501 不可公网访问
```

### 阶段 2：Cloudflare/Caddy

交付：

```text
aob.dipperwoven.com
events.dipperwoven.com
```

验收：

```text
证书有效
HTTPS 正常
WebSocket 正常
Host/Path 路由正确
```

### 阶段 3：容器

交付：

```text
caddy
dashboard
control-api
public-ingress
```

验收：

```text
容器重启后数据库保留
healthz 正常
Auth0 登录正常
Private API 不公网开放
```

### 阶段 4：自动部署

交付：

```text
GitHub Actions
ARM64 image build
Registry push
OCI remote deploy
health check
rollback
```

验收：

```text
GitHub 推送后自动部署
镜像 tag 可追踪
失败不会替换稳定版本
回滚可用
```

### 阶段 5：真实 Kaggle

交付：

```text
一次 100-unit Kaggle Run
Scaling Events
Trace Upload
Trace API 查询
Dashboard 展示
```

## 12. 真实 Kaggle 验收

使用包含 Trace Upload 修复的版本：

```text
34505bf3ca3e8406b278e2f9c86d2dfe382b40cc
```

Kaggle 日志应包含：

```text
TRACE CHECK: PASSED
Worker Task Spans: 100
LLM Inference Spans: 100
Validation Spans: 100
```

上传日志应类似：

```text
TRACE UPLOAD: batch 0 accepted 200 duplicates 0 rejected 0
TRACE UPLOAD: batch 1 accepted 108 duplicates 0 rejected 0
TRACE UPLOAD: complete 308 accepted 308 duplicates 0 rejected 0
```

API 查询：

```text
GET /api/demo/{run_id}/trace-summary
GET /api/demo/{run_id}/llm-calls?limit=250&sort=start
GET /api/demo/{run_id}/units/{unit_id}/trace
```

期望：

```text
llm_calls = 100
unique_units = 100
unattributed_calls = 0
finalized = true
```

每条 LLM Call 验证：

```text
Unit ID
Worker ID
Attempt
Trace ID
Span ID
Start timestamp
End timestamp
Duration
Input tokens
Output tokens
```

Dashboard 验证：

```text
GPU & Worker Ops
LLM Observability
```

切换 Tab 时确认：

- Run ID 不变化。
- 不创建第二个 Run。
- 不再次调用 `/api/demo/start`。
- 不再次提交 Kaggle Notebook。
- 100 条真实 LLM Call 全部可访问。
- 并发时间条正确重叠。
- Unit ID 与原始 `supervisor.log` 一致。

## 13. 健康检查和安全检查

Public Ingress：

```text
GET https://events.dipperwoven.com/healthz
```

Dashboard：

```text
GET https://aob.dipperwoven.com
```

外部网络检查：

- `8010`、`8011`、`8501` 无法直接访问。
- `/docs` 不对公网开放。
- 旧 `/v1` 路由按生产配置关闭。
- 未签名 Event/Trace 请求被拒绝。
- 错误响应不包含 Secret 或 Notebook 凭据。
- Trace 请求不能上传 Prompt、Response、Customer Data 或任意属性。

## 14. 回滚

每次部署保留：

```text
当前 Git SHA
上一个稳定 Git SHA
镜像 tag
数据库备份
```

回滚步骤：

```text
1. 选择上一个稳定镜像 SHA
2. docker compose pull
3. docker compose up -d
4. health check
5. API smoke test
6. Dashboard 登录测试
```

回滚不应：

- 恢复旧 Event Secret。
- 重新开放旧 `/v1`。
- 暴露内部容器端口。
- 修改数据库到未经验证的旧快照。
- 将 Cloudflare DNS 指向临时主机。

## 15. 风险和限制

### OCI Always Free

- ARM64 实例可能存在容量不足。
- 免费资源没有高可用保证。
- 主机故障会同时影响 Dashboard、API 和 SQLite。
- 账户配额、磁盘、备份和出网费用需要在 OCI 控制台确认。

### SQLite

适合：

```text
单实例
低并发
Demo
内部验证
```

不适合：

```text
多 API 副本
跨节点高可用
高并发生产
长期大量 Trace
```

### Kaggle

依赖：

- Kaggle Notebook Private 权限。
- Kaggle Internet。
- Kaggle API 凭据。
- Kaggle 能访问 Cloudflare HTTPS 域名。
- Cloudflare/Caddy 不阻断 POST。
- Kaggle 配额和 GPU 队列可用。

## 16. 最终上线标准

只有全部满足以下条件后，才可宣布部署完成：

- OCI ARM64 主机稳定运行。
- Cloudflare HTTPS 正常。
- Dashboard Auth0 登录正常。
- Private API 不直接公网暴露。
- Public Ingress HMAC 校验正常。
- GitHub Actions 可以构建和部署 ARM64 镜像。
- 回滚流程通过测试。
- 新的 100-unit Kaggle Run 成功。
- 100 Task Spans 已上传。
- 100 LLM Spans 已上传。
- 100 Validation Spans 已上传。
- `trace-summary.llm_calls == 100`。
- `trace-summary.unique_units == 100`。
- `trace-summary.unattributed_calls == 0`。
- Dashboard 显示 100 条真实 LLM Calls。
- Tab 切换不创建新 Run。
- Trace retention 清理策略已经部署。

