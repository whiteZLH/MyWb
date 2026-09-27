# MyWb

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机可直接使用的 **OpenAI / Anthropic 兼容 API**。

适用场景：

- 用 **Codex CLI** 走 `/v1/responses`
- 用 **Claude Code / CC Switch** 走 `/v1/messages`
- 用 **Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI** 走 `/v1/chat/completions`

[English](#english) · [中文](#中文)

---

## 中文

### 这是什么

`MyWb` 是一个本地协议转换器。它会读取你已经登录好的 WorkBuddy / CodeBuddy 桌面端凭据，按账号所属站点转发到对应后端，然后在本地暴露这些接口：

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

它不负责登录，不模拟桌面端，也不替你执行工具。它只做三件事：

1. 读取本机登录态并注入鉴权头
2. 在 OpenAI / Anthropic 协议和腾讯后端协议之间转换
3. 对 `Codex CLI` 这类长上下文 agent 请求做后端友好的压缩投影

> 命名说明：项目对外名称现在叫 `MyWb`（仓库 `whiteZLH/MyWb`）。代码里仍保留部分历史命名，比如 `codebuddy2openai`、`CODEBUDDY2OPENAI_*`，目的是兼容旧配置和环境变量。

### 你能用它做什么

- 把 WorkBuddy 订阅复用到 OpenAI 兼容客户端
- 让 Codex CLI 直接接腾讯后端，而不是只接 OpenAI 官方
- 让 Claude Code 通过 CC Switch 复用 WorkBuddy 支持的模型
- 保留原生 `tools` / `tool_calls` / 流式 SSE / 多轮工具调用

### 当前支持

| 客户端 / 协议 | 接口 | 当前状态 |
|------|------|------|
| OpenAI Chat Completions | `/v1/chat/completions` | 已支持 |
| OpenAI Responses | `/v1/responses` | 已支持，适配 Codex CLI |
| Anthropic Messages | `/v1/messages` | 已支持，适配 Claude Code / CC Switch |
| OpenAI Models | `/v1/models` | 已支持 |
| Health Check | `/health` | 已支持 |

## 支持站点

按凭据身份自动路由，四个象限的端点、身份头、模型目录与计费主机互相独立：

| profile | 账号来源 | chat 端点 | 计费主机 |
|---|---|---|---|
| `cn-cli` | 国内 CodeBuddy | `copilot.tencent.com` | `www.codebuddy.cn` |
| `cn-work` | 国内 WorkBuddy | `www.workbuddy.cn` | `www.workbuddy.cn` |
| `intl-cli` | 国际 CodeBuddy | `www.codebuddy.ai` | `www.codebuddy.ai` |
| `intl-work` | 国际 WorkBuddy AI | `www.workbuddy.ai` | `www.workbuddy.ai` |

模型目录优先用当前账号凭据从上游实时拉取：国内与国际目录不重叠（国际站含 `gpt-5.6-*`、`gemini-3.5-flash` 等），拉取失败时按站点回落到内置列表。管理后台添加账号时可选择登录站点。

---

## 3 分钟上手

### 1. 前置条件

你需要先满足这 3 个条件：

1. 本机已经安装并登录 **WorkBuddy / CodeBuddy** 桌面端
2. 本机有 **Python 3.8+**
3. 已安装依赖 `fastapi`、`uvicorn`、`httpx`

默认会在这些位置寻找登录态：

- macOS: `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/*.info`
- Windows: `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
- Linux: `~/.local/share/CodeBuddyExtension/Data/Public/auth/*.info`

> 国内与国际账号的 `.info` 可能落在同一目录；转换器按凭据自身的 `domain` / JWT 签发方判断站点，无需你手动指定。多账号场景建议用管理后台（`admin/`）导入，可显式选择站点并在授权时校验归属。

### 2. 安装依赖

推荐用 `uv`：

```bash
git clone https://github.com/whiteZLH/MyWb.git MyWb
cd MyWb

uv venv
uv pip install -r requirements.txt
```

也可以用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> 注意：启动服务前必须先装依赖。

### 3. 启动

项目只有一个运行入口：管理后台 `admin.server`（协议转换核心作为 ASGI 应用被它挂载，不再单独运行）。

**一键启动（推荐）**：脚本首次运行会自动生成 `.env`（随机 `ADMIN_KEY` 与客户端 Key），随后启动服务；数据与登录态都落在当前目录。

```bash
# Linux / macOS
bash start.sh

# Windows PowerShell
.\start.ps1
```

重复运行会直接启动，不会覆盖已有 `.env`。想自定义端口或开关，编辑生成的 `.env`（参考 `.env.example`）。

**手动启动**：自行准备 `.env` 再启动。

```bash
cp .env.example .env
# 编辑 .env，填写 ADMIN_KEY（至少 20 字符）与可选的客户端 Key
python3 -m admin.server
```

看到监听 `http://127.0.0.1:8787` 就说明已经起来了。管理界面在 `http://127.0.0.1:8787/admin/`。

> 脱敏默认开启。

### 4. 快速自检

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

如果这两条能通，说明本地服务、登录态、基本路由都没问题。

---

## 客户端接入

### Codex CLI

这是当前最推荐的接法。Codex CLI 走的是 `/v1/responses`，而不是 `/v1/chat/completions`。

确保服务已启动（`python3 -m admin.server`）。

把下面配置合并到 `~/.codex/config.toml`：

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

设置一个占位环境变量：

```bash
export CODEBUDDY2OPENAI_KEY=any-value
```

启动：

```bash
codex --profile workbuddy "你的任务描述"
```

补充说明：

- 脱敏默认已开启（管理后台启动时固定启用 `desensitize`）
- 当前 `/v1/responses` 默认已经会做投影压缩
- 请求日志与统计在管理后台「请求记录 / 运行统计」页查看，不再有 `--log` 参数

### Claude Code / CC Switch

Claude Code 不走 OpenAI 协议，而是走 Anthropic Messages。

确保服务已启动（`python3 -m admin.server`）。

在 CC Switch 里配置：

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

注意：

- 模型名必须填写腾讯后端支持的真实模型名
- 不做 Anthropic 模型名到腾讯模型名的自动映射
- Claude Code 场景依赖默认开启的脱敏

### 其他 OpenAI 兼容客户端

适用于：

- Cherry Studio
- ZCode
- LobeChat
- NextChat
- Open WebUI
- 自己写的 OpenAI SDK 客户端

配置方式：

- Base URL: `http://127.0.0.1:8787/v1`
- API Key: 在管理后台「客户端密钥」中创建的 Key
- 模型名: `glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

---

## 常用命令

### 基本启动

```bash
python3 -m admin.server
```

监听地址与端口、脱敏、日志等由 `.env` 配置（见下文「相关环境变量」），不再有命令行参数。

### curl 示例

```bash
curl http://127.0.0.1:8787/v1/models

curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","messages":[{"role":"user","content":"你好"}]}'

curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"数1到5"}]}'
```

---

## 日志与排障

### 查看请求记录

服务启动后，请求与统计都在管理后台里看：

- 「请求记录」页：逐条查看时间、处理账号、来源、模型、接口、流式与否、首字延迟、总耗时、生成速度与 Token 消耗
- 「运行统计」页：按模型与按账号汇总请求量、成功率与生成速度

> 管理后台入口固定将文件日志关闭（`log_path=None`），排障以页面上的请求记录为准。

### 日志里能看到什么

每条请求在后台记录里都能看到来源、状态与耗时。若需要更详细的请求体/响应体日志，可自行在部署环境中开启对应调试能力。

### 最常见问题

#### 找不到登录文件

说明桌面端没登录，或者登录目录不在默认路径。先确认桌面端已经真正完成登录；也可以在管理后台用浏览器授权直接添加账号。

#### 401

分两种：

- 本地 401：客户端没带管理后台创建的 API Key（在「客户端密钥」页创建）
- 后端 401：腾讯 token 失效，尝试重新打开桌面端登录，或在后台刷新凭据

#### 响应慢

先换快一点的模型，比如 `deepseek-v4-flash`。

#### 被“敏感内容”拦截

这是腾讯后端的内容审核，不一定是用户问题本身敏感，很多时候是 agent runtime 文本触发的，比如：

- `DoS`
- `exploit`
- `credential`
- `sandbox`
- `escalation`
- 竞争品牌词
- tool description 中的安全术语

管理后台默认已开启脱敏，可缓解此类拦截；若仍命中，可在请求记录里定位是哪个模型/接口触发的。

---

## 管理后台

项目附带一个中文 Web 管理后台（`admin/`），它也是服务的运行入口，适合服务器长期运行：

- 浏览器授权添加国内 / 国际 CodeBuddy / WorkBuddy 账号，无需桌面端；多账号自动轮转、冷却与积分耗尽避让
- 凭据刷新、积分余额查询、每日自动签到（默认北京时间 09:00，可配置）
- 客户端 API Key 创建与撤销、真实模型连接测试
- 概览页：请求量、完成成功率、HTTP 成功率、平均耗时与最近 100 条请求

本地启动（需先安装依赖并设置至少 20 位的管理密钥）：

```bash
export ADMIN_KEY=$(python3 -c "import secrets; print(secrets.token_urlsafe(36))")
export CODEBUDDY2OPENAI_KEY=sk-wb-$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
python3 -m admin.server
```

然后访问 `http://127.0.0.1:8787/admin/`。一键 Docker 部署见下文；详细说明见 [ADMIN_README.md](ADMIN_README.md)。

> 管理后台为单进程设计。一键 Docker 部署默认监听所有网卡（`network_mode: host`，或端口映射也可），**局域网内任意设备都能打开管理后台，且为明文 HTTP**；跨不可信网络务必放在 HTTPS 反向代理之后（示例：`deploy/admin/nginx.conf.example`）并设置 `ADMIN_SECURE_COOKIE=1`。

---

## Docker 部署

如果你更习惯用 Docker，可以直接用。

前提是把宿主机登录态目录挂进去，因为容器里拿不到桌面端 auth 文件。

### 一键部署（推荐）

一条命令完成密钥生成、镜像构建与启动（脚本为 POSIX sh，OpenWrt 等无 bash 环境可直接运行）：

```bash
sh deploy/one-click/deploy.sh
```

脚本会自动生成 `ADMIN_KEY` 与客户端 Key，首次部署时会打印一次（保存在 `$MYWB_HOME/.env`，默认 `/opt/mywb/.env`），随后访问 `http://127.0.0.1:8787/admin/`（同网段设备可用脚本打印的局域网地址访问）。改端口：编辑 `$MYWB_HOME/.env` 里的 `PORT` 后重跑脚本；停止：`docker compose -f deploy/one-click/docker-compose.yml --env-file /opt/mywb/.env down`。

数据默认落**系统目录**（`/opt/mywb`，可用 `MYWB_HOME` 覆盖），与代码仓库分离，避免 `git clean` / 删除仓库 / 重新 clone 时连数据与密钥一起丢失。

| 宿主机（默认） | 容器内 | 内容 |
|--------|--------|------|
| `/opt/mywb/.env` | `/app/.env`（只读） | 配置；容器启动时由 `core/settings.py` 自动加载，`PORT` 等开关均生效 |
| `/opt/mywb/auth` | `/data/auth` | 账号登录态（`*.info` 凭据） |
| `/opt/mywb/management` | `/data/management` | SQLite 数据库 `mywb.db`、账号索引、API Key 摘要、请求统计 |

> 数据默认目录在 OpenWrt 上选的是 `/opt/mywb`（overlay/extroot 持久化）而非 `/var` —— 部分 OpenWrt 的 `/var` 是 tmpfs，重启即失。用 `MYWB_HOME=/your/path sh deploy.sh` 可改；把 `MYWB_HOME=/your/path` 写入 `/etc/mywb/mywb.env` 可持久生效。

> **网络模式**：一键部署使用 `network_mode: host`（容器直接监听宿主机端口，不经 Docker 的 DNAT 转发），以兼容 OpenWrt 等 `fw4`/nftables 防火墙环境——在那些环境下端口映射产生的「转发流量」会被 forward 链丢弃，表现为宿主机能访问、局域网客户端超时。注意 **Docker Desktop（Windows / macOS）对 host 网络支持有限**：此时请把 compose 里的 `network_mode: host` 换回端口映射 `ports: ["${PORT:-8787}:8787"]`。OpenWrt 下若因数据目录在 `/opt` 等路径出现挂载问题，可临时加 `security_opt: ["label=disable"]`。

### docker compose（手动）

先改 `deploy/admin/docker-compose.yml` 里的 auth 挂载路径，再执行：

```bash
cp deploy/admin/env.example .env   # 填写 ADMIN_KEY 与 CODEBUDDY2OPENAI_KEY
docker compose -f deploy/admin/docker-compose.yml --env-file .env up -d --build
```

> `docker compose -f <file>` 默认从**该文件所在目录**读取 `.env`，而非当前目录；因此必须显式加 `--env-file .env`（或把 `.env` 放到 `deploy/admin/` 下）。

### 相关环境变量

取值规则（从高到低）：显式参数 → 进程环境变量 → `.env` 文件 → 默认值（当前运行目录）。

服务会自行加载当前运行目录下的 `.env`（可用 `MYWB_ENV_FILE` 指定其它文件，`MYWB_SKIP_DOTENV=1` 关闭），不必经过 `start.ps1` / `start.sh`。

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `ADMIN_KEY` | 管理后台密钥，至少 20 字符（**必填**，缺失则启动失败） | 无 |
| `CODEBUDDY2OPENAI_KEY` | 首次迁入的客户端 API Key；初始化后以库内密钥列表为准 | 空 |
| `ADMIN_DIR` | 数据目录：SQLite 数据库 `mywb.db` 与凭据元数据 | 当前运行目录 |
| `MANAGEMENT_DATA_DIR` | 同 `ADMIN_DIR`（兼容旧部署）；两者都设置时 `ADMIN_DIR` 优先 | — |
| `CODEBUDDY_AUTH_DIR` | 登录态目录（设置后不再回落到桌面端自动发现） | `<当前运行目录>/auth`，未找到时回落桌面端目录 |
| `PORT` | 监听端口 | `8787` |
| `ADMIN_SECURE_COOKIE` | 会话 Cookie 是否加 `Secure` 标志；HTTPS 反代对外时建议设 `1` | `0`（关闭，便于纯 HTTP / 局域网访问） |
| `CODEBUDDY_REASONING` | 是否向前端透出模型思考过程；设 `0` 关闭 | `1` |

---

## 思考过程（reasoning / thinking）

部分模型（如 `deepseek-v4.1-flash`、`deepseek-v4-pro`）会在流里返回思考过程。上游字段名不统一，转换器会依次识别 `delta.reasoning_content`、`delta.reasoning`、`delta.thinking` 等，并按各协议标准形态输出：

| 协议 | 输出形态 |
|------|------|
| `/v1/chat/completions` | 流式原样透传上游思考分片；非流式聚合进 `choices[].message.reasoning_content`（DeepSeek 官方约定） |
| `/v1/responses` | 新增 `output[].type = "reasoning"` 项，配套 `response.reasoning_summary_text.delta/done` 事件；`usage.output_tokens_details.reasoning_tokens` 按实际填写 |
| `/v1/messages` | 转为 Anthropic `thinking` content block，配套 `thinking_delta` / `signature_delta` 事件 |

关闭开关：设置 `CODEBUDDY_REASONING=0` 后，`/v1/responses` 与 `/v1/messages` 不再合成思考内容（`/v1/chat/completions` 流式仍会原样转发上游内容）。

反向：Anthropic 客户端的 `thinking` 请求参数会映射为 Chat 的 `reasoning_effort`（`budget_tokens` > 16384 → `high`，> 4096 → `medium`，其余 `low`）。

> 验证：`python3 tests/test_reasoning.py`

---

## 模型列表

模型列表优先用当前账号凭据从上游 `/v3/config` 实时拉取，本机装有 WorkBuddy 时回落到其动态目录，最后使用内置兜底列表。

内置兜底列表按站点区分，两站目录不重叠：

- 国内（`cn-cli` / `cn-work`）：`hy3`、`hy4-preview`、`kimi-k3`、`kimi-k2.8-preview`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`、`glm-5.1`、`glm-5v-turbo`、`kimi-k2.7`、`kimi-k2.6`、`kimi-k2.5`、`deepseek-v4-pro`、`deepseek-v4.1-flash`、`deepseek-v4-flash`、`minimax-m3-pay`、`hy3-preview-agent`、`auto`
- 国际（`intl-cli` / `intl-work`）：`default-model`、`fast-model`、`balanced-model`、`primary-model`、`deep-model`、`kimi-k3`、`kimi-k2.8-preview`、`kimi-k2.6`、`deepseek-v4.1-flash`、`deepseek-v4.1-flash-sg`、`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、`gpt-5.5`、`gpt-5.4`、`gemini-3.5-flash`、`glm-5.3`、`glm-5.2`、`hy4-preview-f`、`hy4-preview`、`hy3`、`auto`

> 具体能不能用，取决于你的订阅与上游状态。以 `GET /v1/models` 返回的实时目录为准。

---

## 项目结构

```text
MyWb/
├── core/                      # 协议转换核心
│   ├── converter.py           # ASGI 应用（由 admin.server 挂载）
│   ├── responses_adapter.py   # OpenAI Responses ↔ Chat 适配
│   ├── responses_projection.py# Codex / agent 请求投影压缩
│   ├── anthropic_adapter.py   # Anthropic Messages ↔ Chat 适配
│   ├── site_routing.py        # 站点路由：国内/国际 × CodeBuddy/WorkBuddy
│   └── desensitize.py         # 运行时文本压缩与零宽脱敏
├── admin/                     # 中文管理后台（运行入口，python3 -m admin.server）
│   ├── server.py              # 管理 API、会话与静态页面
│   ├── pool.py                # 账号池轮转、冷却、积分与签到
│   ├── browser_login.py       # 浏览器授权登录
│   ├── metrics.py             # 请求统计
│   └── static/                # 前端资源
├── deploy/                    # 部署
│   ├── admin/                 # 管理后台（Dockerfile + compose + nginx 示例）
│   └── one-click/             # 一键式部署（deploy.sh 自动生成密钥并启动）
├── tests/                     # 单元测试（pytest）
├── codex-codebuddy.example.toml
├── README.md
└── LICENSE
```

运行测试：

```bash
python3 -m pytest tests/
```

站点路由的专项测试在 `tests/test_site_routing.py`。

---

## 致谢

本项目基于 [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) 的思路演进而来，感谢原作者的开源贡献。

## 免责声明

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

## 开源协议

[MIT](./LICENSE)

---

<a name="english"></a>
## English

`MyWb` exposes your already logged-in **WorkBuddy / CodeBuddy** desktop session as local **OpenAI- and Anthropic-compatible APIs**.

Endpoints, identity headers, model catalogs and billing hosts are selected per credential across four quadrants (`cn`/`intl` × CodeBuddy/WorkBuddy) — see the 支持站点 table above.

Supported endpoints:

- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/messages`
- `GET /v1/models`
- `GET /health`

Recommended use cases:

- **Codex CLI** via `/v1/responses`
- **Claude Code / CC Switch** via `/v1/messages`
- **Cherry Studio / ZCode / LobeChat / Open WebUI** via `/v1/chat/completions`

### Quick Start

```bash
git clone https://github.com/whiteZLH/MyWb.git MyWb
cd MyWb

uv venv
uv pip install -r requirements.txt

cp .env.example .env   # 填写 ADMIN_KEY（至少 20 字符）
uv run python -m admin.server
```

Then verify:

```bash
curl http://127.0.0.1:8787/health
curl http://127.0.0.1:8787/v1/models
```

### Codex CLI

Use `/v1/responses` (desensitization is on by default).

```toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

Run:

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "your task"
```

### Claude Code / CC Switch

Use `/v1/messages`:

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

### Notes

- desensitization is enabled by default in the admin console
- `/v1/responses` already applies backend-facing projection by default
- the Chinese web admin console (`python3 -m admin.server`, see [ADMIN_README.md](ADMIN_README.md)) is the single entry point and adds browser-based account authorization, multi-account rotation, client API keys, daily check-in and request metrics; one-command Docker setup: `sh deploy/one-click/deploy.sh`

### Disclaimer

For personal learning and research only. Not affiliated with Tencent, WorkBuddy, CodeBuddy, OpenAI, or Anthropic.

---

<sub>
Keywords: MyWb · codebuddy to openai · codebuddy2openai · workbuddy api proxy · workbuddy openai adapter · codex cli workbuddy · claude code workbuddy · tencent code assistant openai compatible api
</sub>
