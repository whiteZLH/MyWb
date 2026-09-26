# MyWb 中文管理后台

在原 MyWb 转换器上增加独立管理层，部署后访问 `https://你的域名/admin/`。

## 功能

- 默认概览页：可用账号、已知总积分、完成请求数、完成成功率、HTTP 成功率、失败/进行中、平均耗时及最近 100 条请求。
- 导入桌面端 `.info` / JSON 凭据，设置备注，启用、停用、删除账号。
- 浏览器授权登录国内 / 国际 CodeBuddy / WorkBuddy，无需桌面端。生成官方登录链接，在浏览器完成授权后自动获取并保存账号；同一账号再次登录更新原凭据。
- 保存多个账号，默认轮流分配新请求；可切换到手动指定模式。暂停、冷却和最近确认积分耗尽的账号不参与轮转。
- 手动刷新凭据、查询当前周期剩余积分；支持单账号与批量签到，每日自动签到可配置时间（北京时间，默认 09:00）。
- 查看访问令牌到期时间。到期状态来自本地凭据，是否能刷新及调用成功以真实模型测试为准。
- 新建和撤销客户端 API Key。新密钥只显示一次，服务端仅持久化 SHA-256 摘要。
- 中文接入指南和真实模型调用测试；测试最多等待 90 秒，生成预算上限 1024 tokens，以便推理模型完成思考并输出正文。
- 独立管理密钥；12 小时 HttpOnly / Secure / SameSite=Strict 会话 cookie，CSRF 校验，登录失败限流，上传请求最大 1 MB。
- 请求记录页逐条展示调用时间、处理账号、来源、密钥展示名、模型与接口、流式 / 非流式、首字延迟、总耗时、生成速度（tok/s）与 Token 消耗；运行统计页另按账号汇总请求量与生成速度。管理会话与统计数据一并落库，重启不丢。

添加账号默认使用浏览器授权，也可切换到文件导入。授权链接有效期 5 分钟，完成后无需粘贴回调；新账号不会替换已选择的当前账号。授权前需在“登录站点”里选择与账号订阅一致的站点（国内 / 国际 × CodeBuddy / WorkBuddy），服务端会校验上游返回的凭据归属，不一致时拒绝写入。原账号和 API Key 首次启动自动迁入；旧版 `management/state.json` 与 `metrics.json` 会在首次启动时导入数据库，成功后删除原文件。

## 文件

- `admin/server.py`：后台 API、持久化存储、客户端鉴权及转换器适配。
- `admin/db.py`：标准库 sqlite3 持久化层。`mywb.db` 保存账号索引、密钥摘要、账号池设置、账号状态、会话绑定、管理会话、按天统计与请求记录；旧 `state.json` / `metrics.json` 在首次启动时迁移后删除。零新依赖。
- `admin/browser_login.py`：WorkBuddy 授权状态创建、独立 cookie 会话、轮询和令牌交换。授权会话绑定管理登录，令牌不返回浏览器；超时、取消、登出后清理。
- `admin/pool.py`：请求级凭据绑定、轮转、冷却、积分查询与每日签到。`tests/test_account_pool.py` 验证并发隔离、轮转、签到防重复、精确积分及分页。
- `admin/metrics.py`：请求统计与请求记录；按天聚合与最近 100 条记录写入 SQLite，不存储请求正文、回复或密钥。
- `admin/static/`：无外部 CDN 依赖的中文界面。
- `tests/test_admin_server.py`：鉴权、CSRF、账号生命周期、密钥撤销、重启持久化、上传限制和限流测试。
- `tests/test_db.py`：数据库读写、请求记录裁剪、按天统计增量写入与旧 JSON 迁移测试。
- `deploy/admin/Dockerfile`：自包含镜像，构建协议转换核心与管理层，不修改模型转换逻辑。

## 配置与运行

取值规则（从高到低）：

1. 显式参数（函数 / 命令行传入）
2. 进程环境变量
3. `.env` 文件（`python -m admin.server` 会自行加载，不再依赖 `run.ps1`）
4. 默认值 —— 一律以**当前运行目录**为基准

`.env` 查找顺序：`$MYWB_ENV_FILE` 指定的文件 → `<当前运行目录>/.env`。其中相对路径一律相对当前运行目录解析。设置 `MYWB_SKIP_DOTENV=1` 可关闭自动加载（测试用）。

| 变量 | 用途 | 默认值 |
|---|---|---|
| `ADMIN_KEY` | 独立管理密钥，至少 20 字符，建议随机生成 | 无（必填） |
| `CODEBUDDY2OPENAI_KEY` | 首次迁入的客户端 Key；初始化后从持久化状态读取密钥列表 | 空 |
| `ADMIN_DIR` | 数据库 `mywb.db` 与凭据元数据目录 | 当前运行目录 |
| `MANAGEMENT_DATA_DIR` | 同 `ADMIN_DIR`，兼容旧部署；两者都设置时 `ADMIN_DIR` 优先 | — |
| `CODEBUDDY_AUTH_DIR` | 登录凭据目录 | `<当前运行目录>/auth` |
| `PORT` | 监听端口 | `8787` |

容器部署仍可沿用旧值：`deploy/admin/docker-compose.yml` 显式传入 `CODEBUDDY_AUTH_DIR=/data/auth`、`MANAGEMENT_DATA_DIR=/data/management`，显式环境变量优先级更高，行为不变。

生产使用一个 Uvicorn 进程。SQLite 以单连接串行写入，账号选择游标仍缓存在进程内，不适用于直接增加多 worker。Nginx 终止 HTTPS，后端端口仅绑定服务器回环地址。管理会话落库，重启后仍有效，最长 12 小时。

运行入口：`python3 -m admin.server`。协议转换核心（`core/converter.py`）不再提供独立运行入口，只作为 ASGI 应用由管理后台挂载。

新环境部署：

```bash
cp deploy/admin/env.example .env
# 编辑 .env，分别填写随机管理密钥和客户端 API Key
docker compose -f deploy/admin/docker-compose.yml up -d --build
```

配置 Nginx HTTPS 后再登录：管理会话使用 Secure Cookie，不支持通过普通 HTTP 登录。可参考 `deploy/admin/nginx.conf.example` 配置反向代理和 `/responses` 兼容入口。该文件是 server 块内部的片段，需要自行配置域名和 TLS 证书。

账号和管理数据均挂载持久化，重建容器不会丢失。不要把 `.env`、`auth/` 或 `management/` 上传到 GitHub。新环境没有凭据时，可以先登录后台再导入账号。

## 数据持久化（SQLite）

管理数据统一存放在 `ADMIN_DIR/mywb.db`（SQLite，标准库 sqlite3，无额外依赖）：

| 表 | 内容 |
|---|---|
| `accounts` / `api_keys` | 账号索引（含凭据文件名）、客户端密钥 SHA-256 摘要与提示 |
| `pool_settings` / `account_status` / `session_bindings` | 调度方式、签到时间、各账号积分与冷却、会话到账号的绑定 |
| `sessions` | 管理后台会话（12 小时），重启后无需重新登录 |
| `metric_days` | 按北京时间日历日 × 模型的聚合计数与首字延迟直方图（保留 90 天），另含按账号维度的聚合 |
| `request_log` | 最近 100 条请求记录：时间、来源、状态、耗时、首字延迟、是否流式、处理账号、密钥展示名与 Token 计数 |

账号凭据仍以 `auth/*.info` 原文件保存（`CredentialManager` 直接读写该文件，入库存放收益有限且会牵连刷新与浏览器授权链路）。

写入策略：配置类数据（账号 / 密钥 / 设置）在事务里整表快照写回；请求记录与按天统计逐条增量 UPSERT，这是 SQLite 相对 JSON 的主要收益。连接启用 WAL，文件权限尽量收紧为 `0600`。

隐私约束不变：数据库只保存数字与密钥**展示名**，不保存提示词、回复内容或完整密钥。请求记录里 `key_label` 是创建密钥时填写的名称（测试密钥显示为「连接测试」），不是密钥本身。

首次启动若存在旧版 `state.json` / `metrics.json`，会导入数据库并删除原文件（`state.json` 由 `Store`、`metrics.json` 由 `RequestMetrics` 各自处理）；若数据库已初始化，遗留 JSON 直接清理，不会覆盖库内数据。旧数据如需保留，请在升级前自行备份。

## 模型目录更新

在原列表上补充 `hy3`、`hy4-preview`、`kimi-k3`、`glm-5.3`、`glm-5.3-flash`、`deepseek-v4.1-flash` 和 `kimi-k2.8-preview`。API 与管理后台共用 `core/converter.py` 中的模型列表。

这些名称不保证每个账号都有调用权限。2026-09-12 的验证中，`kimi-k2.8-preview` 在上游目录可见，但两个账号调用均返回 11102（模型服务不存在）；此项只加入列表，尚未验证调用成功。其余新增模型曾完成最小调用验证，可用性随上游变化。

## 验证

```bash
# 在仓库根目录运行（推荐）
python3 -m pytest tests/ -q

# 或指定单个后台模块
cd tests && PYTHONPATH=.. python3 -m unittest test_admin_server test_browser_login test_account_pool test_request_metrics test_site_routing
node --check admin/static/app.js
```

6 组后台测试在本机通过。`tests/test_site_routing.py` 覆盖四象限路由、国际站首条 system 补齐、站点一致性校验、目录缓存与 UA 差异。

国际 WorkBuddy AI（`intl-work`）已完成真实账号端到端验证：`/v1/models` 返回国际目录（含 `gpt-5.6-luna` 等）、`/v1/chat/completions`、`/v1/messages`、`/v1/responses` 均返回 200，token 刷新成功并轮换 `refreshToken`，积分查询返回余额。国内站点保持既有端点与请求头语义。

浏览器验证本地登录、账号列表和 JSON 导入；公网验证登录页、管理会话、现有客户端 Key、新 Key 创建与撤销。

## 数据与恢复

删除账号从状态索引移除，并在 `management/trash/` 保留恢复副本；为保护正在进行的令牌刷新，原凭据文件也保留，但不会再次被状态索引选择。不提供网页回收站。需要永久清理时另行处理。

完整账号令牌不返回前端，测试记录不保存消息和回复。除新增 Key 的一次性响应外，完整客户端 Key 不可查看。管理密钥与客户端 Key 用途不同。

## 站点与路由

账号按凭据身份分为四个象限，端点、身份头、模型目录与计费主机都随象限切换（`core/site_routing.py`）：

| profile | 账号来源 | chat 端点 | 计费主机 |
|---|---|---|---|
| `cn-cli` | 国内 CodeBuddy | `copilot.tencent.com` | `www.codebuddy.cn` |
| `cn-work` | 国内 WorkBuddy | `www.workbuddy.cn` | `www.workbuddy.cn` |
| `intl-cli` | 国际 CodeBuddy | `www.codebuddy.ai` | `www.codebuddy.ai` |
| `intl-work` | 国际 WorkBuddy AI | `www.workbuddy.ai` | `www.workbuddy.ai` |

路由依据是凭据的 `auth.domain` 与 `accessToken` 的 JWT `iss`。`copilot.tencent.com` 是国内 CLI 与 WorkBuddy 共用的插件端点，不能单独用来区分产品线，因此仅凭它无法判断时回落到 `cn-cli`。两个提示互相冲突时按凭据无效处理并提示重新授权。

模型目录优先用当前账号凭据从上游 `/v3/config` 实时拉取；国内与国际目录不重叠（国际站包含 `gpt-5.6-*`、`gemini-3.5-flash` 等），拉取失败时按站点回落到内置列表。上游会校验 UA 中的产品版本，因此身份头不能只用通用 UA。

国际站要求请求首条消息为 `system`，否则返回 11128；转换器会在首条不是 system/developer 时补一条中性 system。`developer` 角色在国际站也会被拒，统一映射为 `system`。

## 浏览器授权

1. 管理后台 → 添加账号 → 浏览器登录 → 选择登录站点 → 生成登录链接。
2. 点击“打开官方登录页面”，自行完成扫码或账号登录。
3. 保持添加账号窗口打开，服务器每隔约 3 秒检查授权结果。成功后账号自动出现在列表。

该流程基于腾讯 `/v2/plugin/auth/state`、`/v2/plugin/auth/token` 和 `/v2/plugin/login/account` 接口，国际站使用同构路径，不需要额外开放回调端口。协议实现参考 Sliverkiss/cpa-plugin 的浏览器授权流程，代码独立实现。上游接口变更可能影响登录。

只有已登录的管理员可以创建、轮询和取消自己的授权会话。服务端校验官方登录地址和上游 state 一致性；cookie 和 token 留在服务端。重新生成链接会取消同一管理会话此前的授权等待。服务重启会失效所有未完成的授权。

## 账号池

首次升级后默认启用轮转和北京时间 09:00 自动签到，可在页面底部修改。服务端每 5 分钟查询启用账号的积分及签到状态，页面每 30 秒刷新缓存视图；“刷新状态”主动查询上游。暂停账号不参与后台批量任务。

积分优先使用上游 Precise 小数字段和当前周期剩余额度，分页汇总所有未过期积分包。查询失败保留历史数值，10 分钟以上的快照标为待刷新；无数据不等于零积分。额度耗尽只影响分配，不删除账号。自动签到按上游当日状态避免重复，错过时间可补查，失败最多每 30 分钟再次尝试，成功日期持久化。

请求通过 ContextVar 绑定独立凭据，各账号 CredentialManager 缓存和刷新锁共享。上游 401/403 触发 5 分钟冷却，402/429 触发 30 分钟冷却。后续请求跳过冷却账号；已发送的流式请求不会重放，避免重复消费或重复工具输出。凭据准备失败时尝试另一个可用账号。没有可用账号返回 503。管理页连接测试使用手动指定的测试账号。

每个账号可单独设置并发上限（账号列表「并发」按钮，0 表示不限）。达到上限的账号在轮转中被跳过并显示为「并发已满」，新请求分配到其它可用账号；全部账号满员时立即返回 503 而不排队。请求结束归还名额，会话绑定跟随本次实际分配的账号（原账号暂满落到其它账号后即改绑，不再回到原账号）。

账号池运行入口是 `admin/server.py`，要求单进程部署。

## 概览统计口径

统计 POST `/v1/chat/completions`、`/v1/responses` 和 `/v1/messages`，包括后台模型测试，来源分别标记为 API 和后台测试。模型列表、计数接口、积分查询和其它管理操作不计入。`/responses` 经 Nginx 内部重写后归入 `/v1/responses`。

对话请求数是已结束请求总数，进行中的请求单独显示；未鉴权请求及无可用账号导致的失败也会计数。HTTP 成功率为 2xx 响应占比；完成成功率另外排除流式 error / failed / incomplete 事件、断开、异常和缺少结束标记的流。平均耗时为已结束请求的总耗时均值，包含失败请求。完成成功率不代表语义质量或回复正确率。

统计保存在 SQLite 中，重启不清零。按天聚合保留 90 天，请求记录只保留最近 100 条；不回填升级前记录。页面标明统计起始时间，每 30 秒刷新，请求记录页每 15 秒刷新。
