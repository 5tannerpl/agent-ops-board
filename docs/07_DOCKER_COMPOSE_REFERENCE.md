# AOB Phase 1 — Docker Compose / Caddy 配置参考（ARM64）

**重要**：下面是**实施蓝图而非可直接复制运行的成品**。当前源码还没有完成 `api.private_app` / `api.public_ingress` / Secret 文件读取和 production Streamlit 页面分离。必须先完成 `04_IMPLEMENTATION_BLUEPRINT.md` 再执行部署。

## 1. 预期文件布局

```text
deploy/oci/
  docker-compose.yml
  Caddyfile
  .env.example          # 占位符，绝不含凭证
Dockerfile              # 编译/依赖确认支持 linux/arm64
.dockerignore
/srv/aob/secrets/        # 只在主机，绝不进 Git
/srv/aob/data/           # SQLite persistent
```

## 2. docker-compose.yml 参考

```yaml
name: aob
services:
  proxy:
    image: caddy:2
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    networks: [edge]
    restart: unless-stopped
    security_opt: [no-new-privileges:true]
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}

  dashboard:
    image: aob-dashboard:phase1-arm64
    command: ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
    environment:
      AOB_ENV: production
      AOB_REQUIRE_LOGIN: "1"
      AOB_API_URL: http://control-api:8010
    secrets: [auth0_client_secret, streamlit_cookie_secret]
    networks: [edge, control]
    restart: unless-stopped
    mem_limit: 1536m
    pids_limit: 128
    security_opt: [no-new-privileges:true]
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}

  control-api:
    image: aob-control:phase1-arm64
    command: ["uvicorn", "api.private_app:app", "--host", "0.0.0.0", "--port", "8010"]
    environment:
      AOB_ENV: production
      AOB_DB_PATH: /var/lib/aob/intake.db
      AOB_DEMO_DB_PATH: /var/lib/aob/demo.db
      AOB_EVENT_SECRET_FILE: /run/secrets/aob_event_secret
      AOB_KAGGLE_TOKEN_FILE: /run/secrets/kaggle_api_token
    secrets: [aob_event_secret, kaggle_api_token]
    volumes:
      - aob_data:/var/lib/aob
    networks: [control, egress]
    restart: unless-stopped
    mem_limit: 2048m
    pids_limit: 256
    security_opt: [no-new-privileges:true]
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}

  event-ingress:
    image: aob-ingress:phase1-arm64
    command: ["uvicorn", "api.public_ingress:app", "--host", "0.0.0.0", "--port", "8011"]
    environment:
      AOB_ENV: production
      AOB_DEMO_DB_PATH: /var/lib/aob/demo.db
      AOB_EVENT_SECRET_FILE: /run/secrets/aob_event_secret
    secrets: [aob_event_secret]
    volumes:
      - aob_data:/var/lib/aob
    networks: [edge]
    restart: unless-stopped
    mem_limit: 512m
    pids_limit: 128
    security_opt: [no-new-privileges:true]
    logging: {driver: json-file, options: {max-size: "10m", max-file: "3"}}

networks:
  edge: {}
  control:
    internal: true
  egress: {}  # Control Plane outbound to Kaggle; no public published ports

volumes:
  aob_data: {}
  caddy_data: {}
  caddy_config: {}

secrets:
  aob_event_secret: {file: /srv/aob/secrets/aob_event_secret}
  kaggle_api_token: {file: /srv/aob/secrets/kaggle_api_token}
  auth0_client_secret: {file: /srv/aob/secrets/auth0_client_secret}
  streamlit_cookie_secret: {file: /srv/aob/secrets/streamlit_cookie_secret}
```

**设计注意**：
- Compose 本机 `secrets:` 是文件挂载形式，不等同于 OCI/Swarm 的加密 Secret Store。主机 root / Docker 管理员仍可读取；必须控制宿主机权限。
- `control` 网络为内部网络；`control-api` 同时连接仅出站用途的 `egress` Docker 网络，以便调用 Kaggle API。`egress` 不映射任何宿主端口，也不连接 reverse proxy。Docker bridge 并不是按目的地限制出站的防火墙；若需严格 egress 限制，另设宿主防火墙/出口代理并测试。
- OCI ARM64 支持需要检查 `caddy:2` 和自建镜像的目标架构；自建镜像须构建或发布 ARM64 variant。
- SQLite volume 对 ingress/control 两方可写：这是同机 MVP 妥协，实施时必须加事件数据最小权限设计和事务/锁压力测试。不能将 Docker 网络隔离误当数据库权限隔离。
- 示例未给出生产健康检查、非 root UID、read-only 文件系统、正式限流和完整 Auth0 Streamlit secrets adapter；这些必须在实现阶段增加并测试。

## 3. Caddyfile 参考（只开放事件 POST）

```caddyfile
{
  email admin@example.com
}

dashboard.example.com {
  encode zstd gzip
  reverse_proxy dashboard:8501
}

events.example.com {
  @event_post {
    path /api/events
    method POST
  }
  handle @event_post {
    reverse_proxy event-ingress:8011
  }
  handle {
    respond "Not Found" 404
  }
}
```

该示例只实现 Host/Path/Method 的基本白名单；请求 Body 大小必须由 Event Ingress 强制限制（例如 64KB），也可以在受支持的边缘代理中增加限制；**Caddyfile 不负责替代应用层 HMAC、Replay Check 或 IP 限流**，需部署专用限流插件/独立网关或在 ingress 内实现。在实际部署前执行 `caddy validate --config /etc/caddy/Caddyfile` 检查语法。

## 4. Dockerfile 与产物建议

- Python 使用支持 ARM64 的官方 slim 基础镜像，`FROM --platform` 仅由构建目标控制，生产环境禁止运行 x86 模拟镜像作为默认方案。
- 使用 `pip --require-hashes` 或 lockfile 固定依赖；`python -m compileall`、`pytest` 和依赖扫描进入 CI。
- 使用 `.dockerignore` 将 `.git/`, `venv/`, `.venv/`, `runtime/`, `*.db`, `notebook-check/`, `.env*`, `.streamlit/secrets.toml`, `*.ipynb` 排除。
- 生产构建上下文仅包含需要的运行模块；不要 `COPY . .` 前不做排除。
- 不暴露 Docker TCP socket/2375，不将宿主 `/var/run/docker.sock` 挂入任何应用容器。

## 5. 验证命令（安全检查，不执行敏感数据打印）

```bash
uname -m                            # aarch64
sudo docker compose -f deploy/oci/docker-compose.yml config --quiet
sudo docker compose -f deploy/oci/docker-compose.yml ps
sudo ss -lntup                        # 仅 80/443 + 限定 SSH
sudo docker image inspect aob-control:phase1-arm64 --format '{{.Architecture}}'
# 从外部网络验证 8010/8011/8501 不可达：勿在日志中输出 Token
curl -i https://events.example.com/v1/load-tests
curl -i https://events.example.com/docs
```

注意：`docker compose config` 在部分实现/版本下可能解析 Secret 文件路径或配置值，不要在公开 CI 日志打印完整配置。
