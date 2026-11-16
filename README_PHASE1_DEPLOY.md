# Agent Ops Board — Phase 1 改造文件（旧 Kaggle Notebook）

## 1. 本包的目的

继续使用 Kaggle **现有、已验证** Notebook：`sillypanda5/notebookb061a06458`。

- 本地 Streamlit/Auth0 → FastAPI `/api/demo/start` → `kaggle_launcher.py`。
- Launcher 自动执行 **Kaggle Pull → 临时适配旧 Step 6 的事件回调 → 注入当前 Run 的签名 → Kaggle Push**。
- 旧 Notebook 的 Step 1–5 **不修改**：继续使用原来的 GPU、CUDA llama.cpp、Qwen 数据集和 `llama-server` 参数。
- 保留旧 Step 6 的 `workers.check_model` 和 `modelcheck.py` 实际执行；把 `send_event(event_type, data)` 转接至签名 `aob_emit`。
- Kaggle → Cloudflare Tunnel HTTPS → Windows PC FastAPI `/api/events` → SQLite → Streamlit。

**没有提供真实 Kaggle `.ipynb` 原文件，因此不能离线确认旧 Notebook 的 Step 6 是否符合已知结构。** Launcher 在执行 Pull 后自动检查：必须有一个 `send_event` 函数和四个既有验证事件；匹配失败就停止，不会执行 Push，也不会自行编造模型加载步骤。

## 2. 解压复制

把以下文件复制到已有 `agent-ops-board` 项目根目录、覆盖同名文件：

- `kaggle_launcher.py` — **新改动**；使用旧 Notebook 作为来源，不再依赖本地 `kaggle-notebook/`。
- `demo_service.py` — 用户最新上传的版本，包含 `phase` / `result`。
- `demo_api.py` — 用户最新上传的版本，包含 `/api/events` 和完整的状态响应。
- `demo_auth.py` — 用户最新上传的 HMAC/Auth0 代码。
- `demo_store.py` — 用户最新上传的 SQLite 代码。
- `modelcheck.py` — 用户最新上传的文件，仅供核对，不影响远程 Notebook 自身的版本。

**不要覆盖现有的 `config.py`、`api/main.py`、`live_demo.py`、Auth0 配置或 SQLite 数据库**；这些文件本次没有上传，未被擅自重写。

旧的 `kaggle-notebook/notebookb061a06458.ipynb` 与 `kaggle-notebook/kernel-metadata.json` 不再被这个新版 Launcher 使用，可以先备份，再从 Git 提交中移除。**不要删除 Kaggle 云端 Notebook**。

## 3. 环境配置（Windows PC）

在启动 Uvicorn 的 **同一个 PowerShell** 里：

```powershell
# 替换为当前实际的 Cloudflare Tunnel HTTPS 地址
$env:AOB_EVENTS_URL = "https://YOUR-TUNNEL.trycloudflare.com/api/events"

# 使用你之前已经生成的稳定随机密钥；不要因为重启而随意修改
# $env:AOB_EVENT_SECRET = "<your existing secret>"

# 可选：默认就是此 Notebook
$env:AOB_KAGGLE_NOTEBOOK_REF = "sillypanda5/notebookb061a06458"

python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

另一个 PowerShell 保持 Tunnel 在线：

```powershell
cloudflared tunnel --url http://127.0.0.1:8000
```

另需保证项目已有 `config.py` 暴露以下配置：

- `EVENT_SECRET` ← `AOB_EVENT_SECRET`
- `EVENTS_PUBLIC_URL` ← `AOB_EVENTS_URL`
- `KAGGLE_BIN`、`KAGGLE_ACCELERATOR`、`KAGGLE_PUSH_TIMEOUT`
- `DEMO_DB_PATH`、Auth0 与允许名单配置（保留现有值）

**这不是 Kaggle API 的凭据配置。** Windows 上必须已配置 Kaggle CLI 的认证；Notebook 内不放 Auth0 凭据。

## 4. 本次对旧 Notebook 的具体修改

- 插入第一个 Cell：定义 `AOB_RUN_ID`、`AOB_EVENTS_URL`、本次 Run 的 `_AOB_KEY` 和签名 `aob_emit`。
- 插入第二个 Cell：发送 `notebook_started`（只代表 Notebook 已经开始执行）。
- **只在旧 Step 6 中**将 `def send_event(event_type, data)` 的实现替换为转发到 `aob_emit(event_type, data)`；保留原来的 `sent`/`failed` 统计、测试命令及成功判定。
- 从该 Cell 移除旧 `WEBHOOK_URL` 配置与 `REPLACE_WITH_YOUR_UUID` 检查。
- 旧 Step 6 依次发送四种事件：`model_validation_started`、`worker_validation_completed`、`modelcheck_completed`、`model_validation_completed`。
- 本次最少发送 **5 种事件**。原报告中提到的 `environment_ready`、`model_located`、`model_ready` **不会自动被虚构**；由于没有旧 Notebook 的真实 Steps 1–5，暂未注入这些检查点。将来的 `phase` 仍可根据已有事件显示 `initializing` → `validating` → `validated`。
- Kaggle 的 metadata 保留原有 Dataset 绑定，同时强制 GPU、Internet、双 T4 类别、私有 Notebook，去掉可能造成问题的 `docker_image`。
- 后续 Pull 如果读到上次由 Launcher 提交的 Notebook，自动移除旧的 Run Prelude 和开始事件，再写入当前 Run 的配置；不会积累旧密钥。

## 5. 一键启动操作

当 FastAPI、Tunnel、Streamlit 与 Auth0 都已配置且运行时，从前端点击 **Start Live Demo**。

`POST /api/demo/start` 由 Auth0 授权保护；收到请求后 SQLite 创建 Run，后台提交新的 Kaggle Batch。前端的 `GET /api/demo/{run_id}/status` 能读取实际阶段和验证结果。

如果 Kaggle 旧 Step 6 的实际实现不是已知 `send_event` 模式，将返回明确的 Launcher 错误，需要针对真实 notebook 局部适配，**而不是静默覆盖成猜测代码**。

## 6. 安全及部署限制

- **必须使用私有 Kaggle Notebook**。每次临时提交的 Notebook Cell 含本次 Run 的事件签名密钥。不要公开 Notebook、共享 Kaggle 可读权限、粘贴 Cell 内容或在日志里输出该密钥。
- 临时目录会在命令返回后清理；但已提交到 Kaggle 的版本仍可能保留带密钥的 Cell，故需保持私有并限制事件终态接收和密钥有效期（后者当前接收端尚未强制实现）。
- 当前接收端只检查时间戳和签名，并验证 Run 存在；**没有禁止已完成 Run 继续提交事件**。如果对外开放客户访问，建议另行加强终态/有效期控制。
- `trycloudflare.com` 快速 Tunnel 地址每次可能变化；变更后需要在同一个 FastAPI 进程环境中更新 `AOB_EVENTS_URL` 并重启 API。
- Push 到同一个 Kaggle Notebook 会创建新的 Notebook 版本；请不要和 Kaggle 网页编辑/保存操作并行。
- 本方案使用 Kaggle **Batch**，不恢复 Interactive Session；Batch 结束后 Qwen 服务不再保持在线。

## 7. 本次交付边界

已提供改造后的源码和部署步骤。**按你的要求，没有实际执行 Kaggle Push，没有连接 Auth0，没有进行 HTTPS 端到端测试**。是否能在旧 Notebook 上直接适配，取决于旧 Step 6 代码与文档记录是否一致。
