# AOB Phase 1 — 部署规范、安全门禁与切换计划


> **部署基线 v1.1（2026-11-17）**：明确使用 **Oracle Cloud Infrastructure (OCI) Always Free / Ampere A1 ARM64 / Ubuntu Linux / Docker Compose / 单台公网 VM**；GPU 推理与 Autoscaling 继续留在 **Kaggle**。此设计是拟实施方案，尚未对应用源码完成修改。当前 React+BFF 不在 Phase 1 范围内，先使用 Auth0 保护的 Streamlit 过渡 UI。平台资源按 Oracle 官方当期文档与控制台实际资格核实。详见 `06_OCI_ALWAYS_FREE_DOCKER_DEPLOYMENT.md`。


## 1. 不再使用的部署方式

停止使用 `cloudflared tunnel --url http://127.0.0.1:8000` 直接暴露旧 `api.main:app`，因为这会同时暴露 `/v1/*`、`/api/demo/*` 和 `/api/events`。可以继续使用 Cloudflare/Nginx，但**必须只把公开的 Event 域名路由到独立 Event Ingress 进程**。

## 2. 建议的主机部署方式

```text
Public HTTPS  events.example.com:443
  -> Edge/Reverse Proxy (allow POST /api/events only)
  -> 127.0.0.1:8011 (uvicorn api.public_ingress:app)

Private (VPN/internal network) control-api:8010
  -> uvicorn api.private_app:app
  -> Kaggle CLI + DB

Public HTTPS  dashboard.example.com:443
  -> Streamlit (Phase 1 transitional UI; Auth0 required)
  -> private control-api:8010  [server-side JWT]
```

两个 ASGI app 可以第一阶段共用主机，但公网入站防火墙不能开放 8010/8011 原始端口。若 Streamlit 与 Control Plane 跨主机，使用 VPN/私网 TLS、服务认证；不允许用未受控公网直连。

## 3. 示例 Nginx ingress 规则（示意，按实际平台调整）

```nginx
server {
    listen 443 ssl;
    server_name events.example.com;
    # SSL certificate and TLS hardening configured elsewhere
    client_max_body_size 64k;
    location = /api/events {
        limit_except POST { deny all; }
        proxy_pass http://127.0.0.1:8011;
        proxy_connect_timeout 5s;
        proxy_read_timeout 20s;
    }
    location / { return 404; }
}
```

> 需另配速率限制 zone/WAF；不要将示意配置直接当成完整生产 TLS 配置。FastAPI ingress 本身仍须只注册一个路由；代理配置出错不应让所有业务 API 泄漏。

## 4. 发布前检查（阻断式）

- [ ] Secret 全量轮换：尤其历史 `AOB_EVENT_SECRET`，确认 Auth0/Kaggle 私有权限，必要时更换 Kaggle 凭证。
- [ ] Git 历史 + Release ZIP 均完成 Secret Scanning（gitleaks/trufflehog 一类工具；结果不展示密钥）。
- [ ] `.env`、`.streamlit/secrets.toml`、`allowed_users.json`、运行 DB、附件、Notebook 历史版本、Python venv 不进入发布物。
- [ ] `AOB_ENV=production` 配置缺少时**拒绝启动**，不能 silent fallback。
- [ ] 生产只运行 `api.private_app:app`、`api.public_ingress:app`，不运行旧 `api.main:app`。
- [ ] 测试 Public Host：`/api/demo/*`、`/v1/*`、`/docs`、`/openapi.json`、不允许 HTTP Method 全部拒绝。
- [ ] 测试认证、Run owner、Webhook 终态拒收、字段脱敏、速率/体积限制。
- [ ] Kaggle Notebook Private + 显式可见性核实；代码固定已审查 commit。
- [ ] 修复已知测试文件语法错误，运行所有相关测试和至少一次真实 GPU 验收。

## 5. 推荐实施切换顺序

**T0** 备份当前 DB / 配置；归档日志受限。**T1** 发布双 FastAPI 入口、关闭旧公网 Tunnel。**T2** 轮换 Secret、使历史事件凭证失效；安排在没有运行中的 Notebook 时执行。**T3** 开放仅 Event Ingress 的 HTTPS URL，更新 `AOB_EVENTS_URL`，执行签名测试。**T4** 将过渡 Streamlit 连接到 Private Control Plane，禁止旧 `/v1` 页面。**T5** 运行一遍 Light 和 High GPU Demo。**T6** 删除旧公网入口、关闭暴露端口，持续观察异常事件与资源消耗。

**可回滚**：应用代码可以回滚到已封闭的私网版本；公网暴露旧 `/v1` 和旧 Event Secret 不允许回滚。数据库迁移应支持前向兼容，备份恢复方案提前演练。

## 6. Phase 1 完成定义（Definition of Done）

最小实时 Demo 可正常使用 + 旧 `/v1/*` 在生产路由表中消失 + 公网仅安全入口 + 原有密钥轮换 + Event 终态拒收 + 服务端返回数据白名单 + CI/发布物无敏感文件。满足这些条件才开始 Phase 2 React UI 迁移。


## 7. OCI 免费 VM 生产发布门禁补充（v1.1）

- [ ] OCI Console 已核实 **Always Free Eligible**、home region、A1 ARM64、实际配置和剩余额度；实例可能容量不足或因闲置被回收。
- [ ] `uname -m` 是 `aarch64`；所有 Docker images 的架构为 `linux/arm64`；无 `exec format error`。
- [ ] OCI NSG/Security List 仅 80/443 公网 + 22 限定管理来源；Ubuntu 防火墙及 Docker NAT/iptables 绕行风险单独复测。
- [ ] `docker ps` / `ss -lntup` 只显示 Caddy 对外端口；`8000/8010/8011/8501` 不监听公网。以外部网络实际扫描验证。
- [ ] `events` 公网域名只接受签名 POST，`/docs`、`/openapi.json`、`/v1/*`、`/api/demo/*` 都无法访问。
- [ ] 主机 Secret 目录 0700、Secret 文件 0600 或适当受控 ACL；`docker inspect`、`docker logs`、镜像层与 `docker compose config` 中不含密钥明文。
- [ ] Compose 容器资源配额、日志轮转、自动重启、健康检查、SQLite 持久化挂载、可恢复备份经过验证。
- [ ] 测试 HTTP→HTTPS，证书自动续期，Auth0 Callback 与公开 Dashboard 域名一致。
- [ ] Kaggle 整条链路用生产 Events 域名完成 Light / High Demo 测试。
- [ ] 在 OCI 控制台设置预算/使用量提醒（免费账户也需确认资源级费用规则），并记录域名/备份外部费用。

具体操作顺序见 `06_OCI_ALWAYS_FREE_DOCKER_DEPLOYMENT.md`，示例配置见 `07_DOCKER_COMPOSE_REFERENCE.md`。
