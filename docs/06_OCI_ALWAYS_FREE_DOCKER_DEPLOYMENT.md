# AOB Phase 1 — Oracle Cloud Always Free ARM64 主机部署技术方案

**版本**：1.1 · **状态**：架构与操作设计（尚未执行） · **适用基线**：2026-11-17 `agent-ops-board.zip`

## 1. 确定的前提和边界

- 云服务：**Oracle Cloud Infrastructure (OCI) Always Free**，不是 AWS；推荐 `VM.Standard.A1.Flex` / Ubuntu `aarch64`，一台 VM，Docker Compose。
- ARM64 免费配额：Oracle 官方 Always Free 当前说明约 **2 OCPU / 12 GB 总额**，具体为每月 **1,500 OCPU hours + 9,000 GB hours**，以账户主页、计费页面及控制台显示的 `Always Free Eligible` 为准。可能无可用容量；闲置 VM 可能被回收，不承诺 SLA。
- Block Volume：**200 GB 合计（Boot Volume + Block Volume）**；默认系统盘占用免费配额。
- 可用性：仅单机 MVP，无高可用承诺；不引入付费 ALB/RDS/NAT Gateway/Secrets Manager。
- 云主机只运行管理层与存储；Kaggle 提供 GPU 模型推理和 Worker Autoscaling。用户浏览器不能直接接触 Kaggle Secret。

官方参考：
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm
- https://docs.oracle.com/en-us/iaas/Content/Security/Reference/configuration_tasks.htm

## 2. 推荐主机和资源预算

| 资源 | 建议基线 | 备注 |
|---|---|---|
| Instance | `VM.Standard.A1.Flex` (ARM64) | 家庭区域 Always Free eligible |
| CPU | 2 OCPU 配额内 | 若只拿到 1 OCPU，需要收紧资源上限 |
| RAM | 12 GB 配额内 | 所有实例合计，不要误认为每实例 12GB |
| OS | Ubuntu LTS ARM64 | 先验证 OCI 镜像 Always Free 标签 |
| Root volume | 50 GB 左右 | 含在 200GB 总免费额度中 |
| Docker | Docker Engine + Compose plugin | 不启用 Docker Remote TCP API |
| Database | 单 VM SQLite WAL | Volume 持久化、写冲突及备份测试 |
| HTTPS | Caddy + 自有 DNS 域名 | DNS/域名不保证免费 |

**参考资源分配**（不是强制的运行时保证）：Ubuntu/系统留 2 GB；Control Plane 2 GB；Dashboard 1.5 GB；Ingress 512 MB；Caddy 256 MB；文件缓存/备份/余量约 5 GB。先通过压测观察真实内存；容器 `mem_limit` 是限制而非预留。

## 3. 可信边界 / 网络分区

```text
Internet Browser --443--> Oracle Public IP [Caddy]
                                  | dashboard.hostname -> Streamlit/Auth0
Kaggle signed events --443--------| events.hostname POST /api/events -> Event Ingress
                                  |
                           [Docker private network]
                           Streamlit -> FastAPI Private Control Plane -> SQLite
                                                 -> Kaggle Launcher -> Kaggle API
```

`control-api` 无宿主端口映射，`ingress` 仅接入 reverse proxy；Ingress 和 Control Plane 可以在同机共享持久化 DB volume，但绝不通过 HTTP 对外开放 Control Plane。共享 SQLite 不是强隔离：Ingress 被入侵仍可能修改其有写权限的文件，后续应移到 DB write service/独立数据库。

## 4. OCI VCN / NSG / Ubuntu 防火墙

| 入口 | 目的端口 | 来源 | 说明 |
|---|---:|---|---|
| SSH | 22/tcp | 管理员固定公网 IP `/32` | 首选 OCI Bastion/密钥认证；禁密码认证 |
| HTTP | 80/tcp | 互联网 | ACME 证书与 HTTPS 重定向 |
| HTTPS | 443/tcp | 互联网 | 唯一公网业务入口 |
| FastAPI/Streamlit/DB | 8000/8010/8011/8501/5432 等 | 无公网入站 | 不设规则，不 publish ports |

- OCI VCN Security List 或 NSG 要核对**全部生效规则**；规则是合并放行，不能只看自己新建的一条。
- Ubuntu UFW/nftables 的 Docker 转发规则可能绕开直观的 UFW `INPUT` 规则。最牢靠的首道限制是：**容器根本不发布后端端口**，并从外部机器实测。
- 只使用私有 SSH key；关闭密码登录，系统更新，必要时启用 unattended security updates。
- DNS A 记录分别指向相同公网 IP：`dashboard.example.com`、`events.example.com`，证书由 Caddy 自动取得。以上域名都是占位符。

## 5. 容器与数据设计

1. **reverse-proxy (Caddy)**：唯一发布 80/443，基于 Host + Path + Method allowlist 转发。
2. **dashboard (Streamlit)**：Phase 1 过渡 Auth0 UI，服务端使用 Access Token 调用私有 API；不显示旧 `/v1` 功能。
3. **control-api (FastAPI)**：运行 `api.private_app:app`，用户/Run 权限、Kaggle Launcher、最小 Demo API；无宿主 ports。
4. **event-ingress (FastAPI)**：运行 `api.public_ingress:app`，只注册 POST events、收敛数据 Schema/HMAC，不能访问 Kaggle 凭证。
5. **SQLite Volume**：`/srv/aob/data` 或 Docker named volume，最小读写权限，备份前需要 SQLite Online Backup API 或一致性快照（不要在写入时随意 `cp *.db`）。

用 `tmpfs` 处理可丢弃的临时文件，设置日志轮转、容器非 root 和 `no-new-privileges`。Kaggle CLI 访问路径仅挂入 Control Plane。不要将 `.git/`、Notebook 历史输出、`runtime/`、`venv/`、历史 DB/附件 COPY 到镜像。

## 6. 推荐实施步骤

1. **OCI 准备**：在 home region 创建 Always Free eligible A1 Ubuntu VM，确认 `uname -m` 显示 `aarch64`，配置 SSH Key 和 NSG 规则。
2. **主机加固**：更新系统、创建最小权限 `aob` 用户、安装 Docker + Compose，限制 Docker Socket 权限（docker group 等同 root 级别）。
3. **准备 DNS/TLS**：两个子域名指向 VM；开启 80/443；反代仅公开 Dashboard 和 Event Endpoint。
4. **构建 `linux/arm64` 镜像**：使用带锁定版本的依赖，验证 `docker image inspect` 与组件启动；独立构建与运行镜像更佳。
5. **主机 Secret 注入**：受限 Secret 文件/Compose secrets；修改代码以读取 `_FILE` 文件路径；检查容器权限。轮换已泄漏历史 HMAC 密钥。
6. **启动数据层和 API**：运行 DB migration、启动 Control Plane 与 Ingress，再启动 Streamlit/Caddy；缺少 Secrets 时启动应失败。
7. **外部安全测试**：从另一公网网络检查 Caddy Host/Path/Method 白名单、端口扫描、401/403/404、`/docs` 拒绝、旧 `/v1` 不存在。
8. **真实回归**：Light / High Concurrency，2→8→2 Worker、1→2→1 GPU，确保对 Kaggle 的签名事件稳定送达。
9. **备份恢复演练**：将数据库一致性备份保存到独立安全位置并测试恢复；主机丢失后不能依赖同盘备份。
10. **运维**：系统补丁、凭证轮换、容量监控、无效签名告警、磁盘/内存告警、OCI 费用/资源核查。

## 7. 验收/回滚

- 只开放 `80/443` 公网业务端口（及受限 SSH）；外网不可直连任何 FastAPI/SQLite/Streamlit 容器端口。
- `curl https://events.example.com/v1/load-tests`、`/api/demo/runs`、`/docs` 均拒绝；正确签名且处于 Active 的 Run 才能写入事件。
- 错误响应、容器日志和 UI 不包含 Notebook/GitHub Access Token、历史 HMAC Secret、原始客户数据。
- 通过 ARM64 镜像、Secret 扫描、Docker 安全配置和数据库重启/恢复测试。
- 回滚仅允许回滚容器镜像/控制代码到已通过安全门禁的版本；不得重新开放旧 `/v1` 或恢复泄漏 Secret。

## 8. 已知限制

- Phase 1 **不是** React+BFF：React、HttpOnly Cookie、CSRF 防护在后续阶段交付。
- 只有一台免费 VM，机器故障或 OCI 资源回收时会中断 Dashboard/API；可用性与免费资格无 SLA 保证。
- 单一 SQLite 与复用主机存在故障域和资源争抢；超出 MVP 规模前需升级存储和隔离方案。
- OCI Always Free 在 home region 有配额和容量条件；可能存在域名、额外出网、超额卷、备份等外部成本。上线前逐项核对 OCI 控制台和官方计费政策。
