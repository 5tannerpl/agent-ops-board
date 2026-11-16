# Agent Ops Board — Auth0 私有账号 Live Demo 实施方案

> 版本：MVP v1 · 2026-11-16  
> 目标：只允许**管理员预先创建、密码已指定且进入白名单**的 Auth0 用户访问并启动 Agent Ops Board Demo。**不开放公开注册**。沿用现有 **Streamlit + FastAPI + Kaggle GPU Batch + HTTPS 事件上报**。

## 1. 范围与已验证能力

当前已验证：

- Windows Kaggle CLI 可将 `sillypanda5/notebookb061a06458` 提交为 GPU Batch。
- Kaggle T4 ×2 能运行 Qwen3-4B，使用独立的 `agent-ops-qwen-model` 和 `agent-ops-llama-cuda-t4` Dataset 恢复模型和 CUDA `llama-server`，不必重新编译。
- Kaggle Batch 内的真实模型验证结果已通过 HTTPS POST 成功到达 Webhook.site（四条阶段事件）。

**尚需实现**：自有 FastAPI 事件入口、身份与白名单验证、Run/Events 持久化、Streamlit 展示和受保护的启动按钮。

**边界**：Kaggle **Batch** 不是常驻在线推理服务。客户点击后可以看到排队、运行及推送的事件，但 Batch 完成后不能依赖 `llama-server` 或 Kaggle 进程继续在线。Kaggle 排队时间不可承诺。

## 2. 最小架构

```text
客户浏览器
    │
    ▼
Streamlit（部署在自己的常驻服务器）
    │  Auth0 Universal Login（用户名/密码）
    │  OIDC 登录会话
    ▼
FastAPI Demo Controller
    ├── 验证 Auth0 JWT（签名 / iss / aud / exp）
    ├── Auth0 User ID（sub）白名单检查
    ├── 同时最多允许一个 GPU Batch
    ├── SQLite：Runs / Events
    └── Kaggle CLI push（由后端执行）
                    │
                    ▼
            Kaggle GPU Batch (T4 ×2)
            Qwen3-4B + llama-server + Worker 验证
                    │
                    └── HTTPS POST /api/events
                           独立服务密钥验证
                    ▼
            FastAPI + SQLite → Streamlit 事件展示
```

登录、授权及数据访问权限由你的服务器负责；**Kaggle Notebook 不集成 Auth0，也不得保存客户密码或客户 Auth0 Access Token**。

## 3. Auth0 配置

在 [Auth0 Dashboard](https://manage.auth0.com/) 中：

1. 创建 **Regular Web Application**：`Agent Ops Board Demo`，启用 **Universal Login**。
2. 创建或配置 **Database Connection**：`Agent-Ops-Users`。按当前租户的能力启用用户名登录（常规数据库账号通常同时需要邮箱）；允许管理员创建用户，**关闭自助 Sign Ups**。
3. 仅启用所需 Database Connection；不启用 Google 等 Social Login。
4. 管理员在 **User Management → Users** 中创建账号，填写用户名、邮箱与指定初始密码。建议使用临时初始密码，安全地分发并在首次登录要求修改；不要把密码存入代码或白名单文件。
5. 注册一个 **API**：`Agent Ops Board API`，Identifier / Audience 示例：`https://agent-ops-board-api`；签名算法 RS256。
6. 本地允许回调地址：`http://localhost:8501/oauth2callback`。生产环境改为你的实际 HTTPS 地址（如 `https://demo.example.com/oauth2callback`）。配置 Allowed Logout URLs 与 Web Origins（如应用配置需要）。
7. 按需配置 Brute-force Protection、密码策略、MFA。关闭公开注册**不等于**自动阻止所有已有账号登录；FastAPI 白名单负责最终授权。

> Auth0 界面和字段名称可能随租户功能变化。若要求完全无需邮箱、仅凭用户名登录，需要先核实相应 Identifier 功能是否可用。

### 3.1 白名单规则

以 Auth0 Access Token 中稳定的 `sub`（用户 ID）为主键，而不是用户名：

`allowed_users.json`（仅示例，实际 User ID 从 Auth0 获取）：

```json
{
  "allowed_users": {
    "auth0|REAL_USER_ID_1": {"username": "hiringmanager01", "enabled": true},
    "auth0|REAL_USER_ID_2": {"username": "clientdemo01", "enabled": true}
  }
}
```

授权逻辑：有效 Access Token + 白名单存在且 `enabled=true` ⇒ 允许启动 Demo；其他情况返回 401 或 403。禁用用户时应立即使白名单变更生效（例如每次请求读取或刷新缓存），同时 Auth0 可阻止其后续登录。

## 4. Streamlit 接入 Auth0 登录

安装依赖：

```bash
pip install "streamlit[auth]" fastapi uvicorn requests "PyJWT[crypto]"
```

`.streamlit/secrets.toml` 本地配置示意：

```toml
[auth]
redirect_uri = "http://localhost:8501/oauth2callback"
cookie_secret = "REPLACE_WITH_LONG_RANDOM_VALUE"
expose_tokens = ["access"]

[auth.auth0]
client_id = "YOUR_AUTH0_CLIENT_ID"
client_secret = "YOUR_AUTH0_CLIENT_SECRET"
server_metadata_url = "https://YOUR_AUTH0_DOMAIN/.well-known/openid-configuration"

[auth.auth0.client_kwargs]
scope = "openid profile email"
audience = "https://agent-ops-board-api"
```

> 不提交 `.streamlit/secrets.toml` 到 Git。实际使用前确认已安装 Streamlit 版本支持对应 OIDC 及 Access Token 暴露配置，并检查返回 Token 的 `aud` 是你创建的 Auth0 API；**不能使用 ID Token 调用受保护的 FastAPI**。

Streamlit UI（伪主文件示例）：

```python
import requests
import streamlit as st

API_BASE = "http://127.0.0.1:8000"  # Streamlit 与 FastAPI 同机时

st.title("Agent Ops Board — Private Demo")

if not st.user.is_logged_in:
    st.info("仅限受邀用户；没有公开注册入口。")
    if st.button("Login with Auth0"):
        st.login("auth0")
    st.stop()

if st.button("Logout"):
    st.logout()

# 按实际 Streamlit 版本核对 token 暴露字段。
access_token = st.user.tokens.get("access")
if not access_token:
    st.error("无法取得 API Access Token")
    st.stop()

headers = {"Authorization": f"Bearer {access_token}"}

# 前端的显示不是最终授权；FastAPI 必须独立验证。
me = requests.get(f"{API_BASE}/api/me", headers=headers, timeout=10)
if me.status_code == 403:
    st.error("当前账号不在 Demo 白名单。")
    st.stop()
me.raise_for_status()

if st.button("Start Live Demo", type="primary"):
    response = requests.post(
        f"{API_BASE}/api/demo/start", headers=headers, timeout=15
    )
    if response.status_code == 202:
        st.success("已提交 Demo 启动请求；等待 GPU 调度。")
        st.json(response.json())
    elif response.status_code == 409:
        st.warning("已有 Demo 正在排队或运行。")
    else:
        st.error(f"启动失败：HTTP {response.status_code}")
```

Streamlit 的 `st.login()` 跳转 Auth0 Universal Login；**不要在 Streamlit 自己制作收集密码的表单**。

## 5. FastAPI：JWT + 白名单保护

后端环境变量：

```text
AUTH0_DOMAIN=your-tenant.us.auth0.com
AUTH0_AUDIENCE=https://agent-ops-board-api
```

`demo_auth.py` 核心示例：

```python
import json
import os
from pathlib import Path

import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

DOMAIN = os.environ["AUTH0_DOMAIN"]
AUDIENCE = os.environ["AUTH0_AUDIENCE"]
ISSUER = f"https://{DOMAIN}/"
JWKS = PyJWKClient(f"{ISSUER}.well-known/jwks.json")
BEARER = HTTPBearer(auto_error=False)


def require_user(credentials: HTTPAuthorizationCredentials | None = Depends(BEARER)):
    if credentials is None:
        raise HTTPException(status_code=401, detail="Access Token required")
    try:
        key = JWKS.get_signing_key_from_jwt(credentials.credentials)
        return jwt.decode(
            credentials.credentials,
            key.key,
            algorithms=["RS256"],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iss", "sub", "aud"]},
        )
    except (jwt.PyJWTError, jwt.PyJWKClientError):
        raise HTTPException(status_code=401, detail="Invalid access token")


def require_demo_access(user=Depends(require_user)):
    allowed = json.loads(Path("allowed_users.json").read_text(encoding="utf-8"))
    record = allowed.get("allowed_users", {}).get(user["sub"])
    if not record or not record.get("enabled", False):
        raise HTTPException(status_code=403, detail="Demo access denied")
    return user
```

在 `demo_api.py` 中：

```python
from fastapi import FastAPI, Depends
from demo_auth import require_demo_access

app = FastAPI()

@app.get("/api/me")
def me(user=Depends(require_demo_access)):
    return {"user_id": user["sub"], "can_start_demo": True}

@app.post("/api/demo/start", status_code=202)
def start_demo(user=Depends(require_demo_access)):
    # 调用受控的 Kaggle Launcher，返回真正的 run_id / submitting 状态。
    # 不可直接在这里假装 Kaggle 已启动。
    return {"status": "implementation_pending", "owner_sub": user["sub"]}
```

上面的 `start_demo` **只是身份保护的接入骨架**，不是已实现的 Kaggle 启动逻辑。集成时应使用现有经过验证的 `kaggle kernels push -p <notebook_dir> --accelerator NvidiaTeslaT4`，并执行下述运行状态管理。

## 6. Kaggle 启动与 Run 状态管理

最小 API：

| 接口 | 用途 | 授权 |
|---|---|---|
| `POST /api/demo/start` | 创建 Run、发起 Kaggle CLI Push | Auth0 + 白名单 |
| `GET /api/demo/{run_id}/status` | 查询提交/排队/运行/结束 | Auth0 + 归属检查 |
| `GET /api/demo/{run_id}/events` | 获取运行期间上报的事件 | Auth0 + 归属检查 |
| `POST /api/events` | 接收 Kaggle HTTPS 事件 | 独立 Webhook 认证 |

Run 状态建议：

```text
idle → submitting → queued → running → complete
                   ↘ failed / cancelled
```

- 单实例 MVP 使用 SQLite 保存 `runs`、`events` 和 `owner_sub`，并实现**全局只允许一个活跃 Batch**。不要只依赖 Python 内存锁：服务重启可能丢失状态。
- CLI `push` 返回成功，只能标记“提交已被接受”；**不得**直接标记 GPU Ready。
- 定期查询 `kaggle kernels status`，同步状态；提交失败和队列超时需能恢复。
- Notebook 的 `kernel-metadata.json` 需启用 T4 GPU、保留两个 Dataset、`kernel_sources=[]`，不固定 `docker_image`。所有修改保存为 UTF-8 无 BOM。
- Kaggle CLI 凭据仅由 FastAPI 所在主机保存，不能放入前端或公开 Git 仓库。
- 对任何白名单用户也要限制启动频率，以避免耗尽 Kaggle 配额。

## 7. Kaggle → FastAPI HTTPS 事件

之前的 Webhook.site 已验证 4 个事件可以从 Kaggle Batch POST 出去：

```text
model_validation_started
worker_validation_completed
modelcheck_completed
model_validation_completed
```

正式实现：将 `WEBHOOK_URL` 从 Webhook.site 改为服务器上的 HTTPS `/api/events`。

**不要复用 Auth0 用户密码或用户 Access Token**。用独立的 Webhook Secret（建议 HMAC 请求签名并检查时间戳），在 FastAPI 端验证来源、校验 `run_id`，以 `event_id` 做唯一键去重，并在 SQLite 中持久化。

为避免敏感内容外泄：初期仅发送测试阶段、耗时、计数、结果状态和必要的脱敏 Trace；**不要直接将真实邮件、客户 PII 或完整 LLM Prompt/Response 上报到公共测试端点**。

> 当前事件粒度是阶段级；要展示真正逐任务 Worker 状态，需要在实际 Worker 运行循环中上报，而非只在 `workers.check_model` 完成后发送汇总。

## 8. 项目最小目录

```text
agent-ops-board/
├── app.py                     # 现有 Streamlit 页面
├── demo_api.py                # FastAPI + Demo start/status/events
├── demo_auth.py               # Auth0 JWT + 白名单
├── allowed_users.json         # 授权用户 sub → enabled
├── demo.db                    # SQLite（运行时生成，不提交）
├── .streamlit/
│   └── secrets.toml           # 本地 Secret（不提交）
├── kaggle-notebook/
│   ├── notebookb061a06458.ipynb
│   └── kernel-metadata.json
└── workers/
```

## 9. MVP 验收清单

- [ ] Auth0 Universal Login 可以用管理员创建的用户名与密码登录。
- [ ] 公开注册已关闭；无注册按钮或自助注册路径。
- [ ] 不在白名单的合法 Auth0 用户访问 `/api/demo/start` 返回 403。
- [ ] 无 Token / Token 无效访问受保护 API 返回 401。
- [ ] 白名单用户可以点击 `Start Live Demo`；API 返回 `run_id` 和真实 `submitting` 状态。
- [ ] 同时重复点击不会提交第二个 Kaggle Batch（409）。
- [ ] Kaggle 状态正确变化：QUEUED → RUNNING → COMPLETE/ERROR。
- [ ] Kaggle 向受保护的 `/api/events` 发送真实模型验证事件，SQLite 记录并去重。
- [ ] Streamlit 能只展示该用户有权查看的 Run 和事件。
- [ ] Kaggle 结束后事件与运行记录仍可查询。
- [ ] 审核 Kaggle 使用政策、GPU 配额及 Batch 排队时长，不对客户承诺即时 GPU 可用。

## 10. 最推荐的实施顺序

1. Auth0 创建私有数据库账号、关闭 Sign Ups、配置 Universal Login 和 API Audience。
2. Streamlit 增加 `st.login()` 和登出入口；FastAPI 完成 JWT/JWKS + 白名单验证。
3. 打通 `GET /api/me` 的 401 / 403 / 200 三种情况。
4. 接入现有 Kaggle CLI Launcher，增加持久化 Run 和全局单任务保护。
5. Webhook.site 改为受保护的 FastAPI `/api/events`，验证实时事件写入 SQLite。
6. Streamlit 按 Run 展示事件、状态、真实模型验证结果。

**验收原则：登录成功 ≠ 白名单授权；CLI Push 成功 ≠ GPU 已就绪；模型测试通过 ≠ Webhook 上报已送达。三类状态必须独立验证。**
