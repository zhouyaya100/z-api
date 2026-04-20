# 🛡️ Zapi

轻量级 OpenAI API 网关 v3.6.0，支持多渠道路由、分组策略、配额管理、运营角色。

## 特性
- 🔧 **多渠道路由** — 倒排索引 O(1) 查找，优先级+权重选择，自动 failover
- 👥 **分组策略** — 用户分组 → 渠道授权，不同组看不同模型
- 💵 **双层配额** — 用户级 + 令牌级，批量合并扣减，充值/扣除
- 🔐 **安全认证** — JWT + bcrypt + 图形验证码 + 登录防暴力破解
- 📋 **批量日志** — 异步批量写入，自动清理过期记录
- 🐍 **速率限制** — IP + Token 双维度滑动窗口（429 直接拒绝）
- 📦 **OpenAI 兼容** — /v1/chat/completions, /v1/models, embeddings, audio
- 🎨 **Vue 3 管理面板** — 中英双语，渠道/用户/令牌/分组/日志/用量/报表一站式管理
- 🛡️ **运营角色** — 只读查看平台统计/日志/用量/渠道，同时保留用户功能
- 👑 **超级管理员** — ID=1 不可被其他管理员修改/删除/降级
- 📊 **报表导出** — CSV/XLSX，支持明细和按日/用户/模型/渠道汇总
- 🔄 **模型映射** — 对外名称自由定义，自动映射到上游真实模型名，用户无感知
- 🗄️ **PostgreSQL** — 生产级数据库，连接池优化
- 🔮 **Redis 预留** — 缓存/限流/配额模块均留 Redis 后端接口
- 🔔 **通知中心** — 管理员发送/广播通知，故障自动通知，分类过滤，已读管理
- 💓 **心跳检测** — 自动检测渠道可用性，故障通知+自动禁用+恢复通知

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置数据库

编辑 `config.yaml`：
```yaml
database:
  url: postgresql+asyncpg://user:pass@localhost:5432/zapi
```

### 3. 启动服务

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 65000
# 或使用脚本
./start.bat   # Windows
./start.sh    # Linux/macOS
```

默认管理员：`admin` / `Admin@123`

### 4. 环境变量覆盖（可选）

```bash
export LITEAPI_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/zapi
export LITEAPI_SERVER_PORT=65000
```

## 项目结构

```
z-api/
├── config.yaml              # YAML 配置
├── requirements.txt
├── app/
│   ├── main.py              # 入口 + lifespan
│   ├── config.py            # 配置管理
│   ├── database/            # 数据库层
│   │   ├── base.py          # DeclarativeBase
│   │   └── engine.py        # 连接池 + init_db
│   ├── models/              # 数据模型
│   │   ├── group.py         # 用户分组
│   │   ├── user.py          # 用户
│   │   ├── channel.py       # 渠道
│   │   ├── token.py         # 令牌
│   │   ├── log.py           # 日志
│   │   └── notification.py  # 通知
│   ├── core/                # 纯业务逻辑（零路由依赖）
│   │   ├── security.py      # JWT + 密码 + 验证码 + 防暴力
│   │   ├── auth_models.py   # 授权模型计算（公共函数）
│   │   ├── heartbeat.py     # 渠道心跳检测
│   │   ├── token_count.py   # tiktoken 计数
│   │   ├── cache.py         # 内存缓存（预留 Redis）
│   │   ├── rate_limit.py    # 限流器（预留 Redis）
│   │   ├── log_writer.py    # 批量日志写入
│   │   ├── quota/           # 配额引擎
│   │   │   ├── types.py     # QuotaResult + QuotaStatus
│   │   │   ├── checker.py   # 配额检查（预留 Redis）
│   │   │   └── deductor.py  # 配额扣减（批量合并 + 预留 Redis）
│   │   └── routing/         # 路由引擎
│   │       ├── channel_pool.py  # 倒排索引 + 增量更新
│   │       ├── engine.py    # 路由引擎（模型映射 + URL 构建）
│   │       └── policy.py    # 策略抽象（优先级/轮询/延迟/成本）
│   ├── routers/             # HTTP 路由层
│   │   ├── auth.py          # 认证（JWT/登录/注册/验证码/角色鉴权）
│   │   ├── channels.py      # 渠道管理（CRUD/测试/模型映射/Key脱敏保护）
│   │   ├── users.py         # 用户管理（超级管理员保护/充值/扣费）
│   │   ├── tokens.py        # 令牌管理（CRUD/充值/admin+user双模式）
│   │   ├── logs.py          # 日志查询（admin/operator多维度过滤）
│   │   ├── groups.py        # 分组管理
│   │   ├── settings.py      # 系统设置
│   │   ├── stats.py         # 统计 + 仪表盘（admin/operator/user三级）
│   │   ├── reports.py       # 报表导出（CSV/XLSX，明细+汇总）
│   │   ├── notifications.py # 通知中心（发送/接收/已读/已发送）
│   │   └── proxy.py         # OpenAI 兼容转发
│   └── static/
│       └── index.html       # Vue 3 SPA 前端
```

## 架构设计

### 三层分离

```
Router 层 → 参数校验、HTTP 状态码、序列化
Core 层  → 纯业务逻辑（可被不同 Router 复用）
Model 层 → 纯数据定义
```

### 渠道路由流程

```
请求 → Token验证 → 配额检查 → 渠道池查找 O(1)) → 模型映射 → 上游转发
                                              ↗ model → priority → [channels] 倒排索引
                            group → channel_ids 预计算映射
```

### 角色权限

| 功能 | Admin | Operator | User |
|------|-------|----------|------|
| 平台统计/仪表盘 | ✅ | ✅ 只读 | ❌ |
| 渠道管理 | ✅ 读写 | ✅ 只读 | ❌ |
| 用户管理 | ✅ | ❌ | ❌ |
| 令牌管理(全平台) | ✅ | ❌ | ❌ |
| 日志查询 | ✅ | ✅ 只读 | ❌ |
| 用量分析 | ✅ | ✅ | ✅ 自己 |
| 报表导出 | ✅ | ✅ | ✅ 自己 |
| 我的令牌 | ✅ | ✅ | ✅ |
| 我的用量 | ✅ | ✅ | ✅ |
| 通知中心 | ✅ 读写 | ✅ 只读 | ✅ 只读 |
| 修改超管(ID=1) | ❌ | ❌ | ❌ |

### 认证机制

- **Admin 静态 Token**：首次启动自动生成随机值，用于 API 管理，不代表具体用户
- **JWT Token**：登录后获取，仅包含 user_id（不含 role）
- **DB 实时校验**：鉴权时从数据库查角色，角色变更立即生效
- **超级管理员**：ID=1 不可被任何非超管修改/删除

### 配额扣减流程

```
请求完成 → 内存队列累加 → 定时批量合并 → 一次性 UPDATE
(同一 Token 5s 内多次扣减合并为一条 SQL)
```

失败请求不扣费，只记录日志和渠道 fail_count。

### Redis 接入（未来）

三个模块预留了 `Protocol` 接口和实现占位：

| 模块 | 当前实现 | Redis 实现 |
|------|---------|-----------|
| 缓存 | `MemoryCache` | `RedisCache`（CacheBackend 协议） |
| 限流 | `RateLimiter` | `RedisRateLimiter`（RateLimitBackend 协议） |
| 配额 | DB 原子更新 | Redis INCRBY + 异步落库 |

## 配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `version` | 3.6.0 | 版本号 |
| `server.port` | 65000 | 服务端口 |
| `server.workers` | 4 | Worker 数量 |
| `security.jwt_expire_hours` | 1 | JWT 过期时间（小时） |
| `security.admin_token` | sk-lite-admin-token | 管理员 API Token |
| `database.url` | postgresql+asyncpg://... | 数据库连接 |
| `proxy.timeout` | 120 | 上游超时（秒） |
| `proxy.max_connections` | 1000 | 最大并发连接 |
| `proxy.retry_count` | 1 | 失败重试次数 |
| `cache.ttl` | 30 | 缓存 TTL（秒） |
| `rate_limit.rpm` | 600 | 令牌每分钟限制 |
| `rate_limit.ip_rpm` | 1200 | IP 每分钟限制 |
| `registration.allow_register` | true | 开放注册 |
| `registration.default_token_quota` | 500 | 新用户默认配额 |
| `timezone_offset` | 8 | 时区偏移（小时） |
| `log.retention_days` | 90 | 日志保留天数 |

## API 概览

### OpenAI 兼容

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /v1/chat/completions | Chat 补全 |
| POST | /v1/completions | Text 补全 |
| POST | /v1/embeddings | 向量嵌入 |
| POST | /v1/audio/transcriptions | 语音转文字 |
| POST | /v1/audio/speech | 文字转语音 |
| GET | /v1/models | 可用模型列表 |

### 管理 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /api/auth/register | 注册 |
| POST | /api/auth/login | 登录 |
| GET | /api/auth/captcha | 验证码 |
| GET | /api/auth/me | 当前用户 |
| CRUD | /api/channels | 渠道管理 |
| CRUD | /api/users | 用户管理 |
| POST | /api/users/{id}/recharge | 用户充值 |
| POST | /api/users/{id}/deduct | 用户扣费 |
| CRUD | /api/tokens | 令牌管理 |
| POST | /api/tokens/{id}/recharge | 令牌充值 |
| GET | /api/logs | 日志查询（admin） |
| GET | /api/logs/operator | 日志查询（operator） |
| CRUD | /api/groups | 分组管理 |
| GET/PUT | /api/settings | 系统设置（需 admin） |
| GET | /api/settings/public | 公开设置（注册开关+分组） |
| GET | /api/version | 版本号 |
| GET | /api/stats | 统计数据 |
| GET | /api/dashboard | 仪表盘 |
| GET | /api/stats/usage | 用量分析（admin） |
| GET | /api/stats/usage/operator | 用量分析（operator） |
| GET | /api/my/dashboard | 个人仪表盘 |
| GET | /api/my/tokens | 我的令牌 |
| GET | /api/my/usage | 我的用量 |
| GET | /api/my/models | 我的可用模型 |
| GET | /api/reports/export | 报表导出（admin） |
| GET | /api/reports/export/operator | 报表导出（operator） |
| GET | /api/reports/my/export | 报表导出（user） |
| GET | /api/notifications | 通知列表 |
| GET | /api/notifications/sent | 已发送通知（admin） |
| GET | /api/notifications/unread_count | 未读通知数 |
| POST | /api/notifications | 发送通知（admin） |
| POST | /api/notifications/batch | 批量发送通知（admin） |
| PUT | /api/notifications/{id}/read | 标记已读 |
| PUT | /api/notifications/read_all | 全部已读 |
| DELETE | /api/notifications/{id} | 删除通知 |
| DELETE | /api/notifications/sent/{id} | 删除已发送通知（admin） |

## 更新日志

### v3.6.0 (2026-04-17)

**心跳检测重构**
- 后台线程替代 asyncio task：解决 uvicorn 单 worker 下 asyncio.sleep 不被调度的问题
- 每次检测共用一个数据库引擎：从 3 个引擎降为 1 个，大幅减少连接开销
- 管理员 ID 只查一次，通知写入复用 session factory
- 本机 IP 启动时缓存：不再每次检测跑 ipconfig
- 请求更轻量：max_tokens=1，消息 "ping"，httpx timeout 10s
- 首次延迟 30s→10s，分段 sleep 便于快速退出
- 精简日志输出：只在检测完成时打一行

**通知中心优化**
- 恢复通知（recovery）独立分类：🟢 绿色标识，区别于故障🔴和普通🔵
- 恢复通知内容包含延迟和故障持续时长
- 检测所有渠道（含已禁用）：被禁渠道持续检测，恢复后自动重新启用并发恢复通知
- 禁用渠道首次失败即告警，不用等 2 次
- 通知卡片交互优化：点击自动标记已读，分类彩色 badge，内容 2 行截断
- 未读 NEW 徽章闪烁动画
- 筛选栏独立一行，操作按钮精简
- 发送通知弹窗支持恢复通知分类

**品牌更新**
- 项目名 Z API → Zapi
- 首页副标题：统一模型管理工具
- 静态文件服务：新增 StaticFiles mount，支持 logo 等资源文件

### v3.5.0 (2026-04-17)

**性能优化**
- list_users N+1 修复：批量查询 token_count + 分组 + 授权模型，从 N+1 查询优化为固定 4 条 SQL
- list_groups N+1 修复：批量查 user_count，1 条 SQL 替代 N 条
- stats 接口合并查询：从 12 次独立 SQL 合并为 4 条聚合 SQL
- user_dashboard 合并查询：从 7 条 SQL 合并为 4 条
- authed_models 三处重复逻辑提取为公共函数 `core/auth_models.py`
- 反向映射索引：channel_pool 新增 `_reverse_mapping`，O(1) 替代全量遍历

**安全增强**
- Admin Token 首次启动自动生成随机值，写回 config.yaml
- `/api/settings` GET 加 admin 鉴权，新增 `/api/settings/public` 公开接口
- 登录防暴力破解加 IP 双维度：username + client_ip 同时检查
- JWT 不再写入 role 字段，鉴权全从 DB 实时查询
- 验证码存储加定时清理（5 分钟一次）

**时区修复**
- 导出报表时间从 UTC 转为本地时区（config.yaml `timezone_offset` 配置）
- 日志列表时间转为本地时区
- 仪表盘时间转为本地时区
- 日期筛选本地日期 → UTC 转换
- 按天分组按本地时区截断
- 新增 `timezone_offset` 配置项（默认 8，即 UTC+8）

**前端优化**
- 移动端适配：侧边栏可折叠，卡片网格自适应
- 版本号动态获取：新增 `/api/version` 端点
- 密码重置改为自定义 Modal（替代浏览器 prompt）
- Token Key 创建后加⚠️ 警告提示
- 渠道测试显示测试模型名，无模型时返回错误
- 日志页导出按钮改名「导出日志」区分用量页
- Admin/Operator 仪表盘统一布局：我的信息 + 平台统计分区显示
- Vue CDN 改用国内源（bootcdn.net）

**Bug 修复**
- main.py 中文乱码修复
- serve_ui 版本号从硬编码改为读取 settings.VERSION
- 无分组用户不再硬 403，允许使用不限分组的渠道
- Operator 调用 `/api/settings` 返回 403 修复
- i18n 英文词典多余 `}` 导致白屏修复

**新增功能**
- 🔔 通知中心模块：管理员发送/广播/批量通知，故障/普通分类，已读管理，未读徽章
- 💓 自动心跳检测：每5分钟检测渠道可用性，故障只通知管理员，连续失败自动禁用渠道
- 🗑️ 删除二次确认：所有删除操作改为自定义 Modal（替代浏览器 confirm）
- 📤 已发送标签：管理员可查看已发送通知历史，显示接收人数，可批量删除
- 通知显示发送者用户名

### v3.4.0 (2026-04-17)

**性能优化**
- list_users N+1 修复：批量查询 token_count + 分组，从 N+1 查询优化为固定 4 条 SQL
- stats 接口合并查询：从 12 次独立 SQL 查询合并为 4 条聚合 SQL
- user_dashboard 合并查询：从 7 条 SQL 合并为 4 条
- group_authed 缓存：同组用户共享授权模型计算结果

**前端优化**
- 移动端适配：侧边栏可折叠，卡片网格自适应
- 版本号动态获取：新增 `/api/version` 端点

**Bug 修复**
- main.py 中文乱码修复
- serve_ui 版本号从硬编码 "3.0.0" 改为读取 settings.VERSION

### v3.3.1 (2026-04-16)

**Bug 修复**
- 注册接口 500：`auth.py` 函数体内多余的 `import re` 导致 `UnboundLocalError`
- 前端 `doRegister`/`doLogin` 增加 `if(!res.ok)` 检查
- 用户管理表格 max_tokens 列与 token_quota 列串行

### v3.3.0 (2026-04-16)

**新增**
- 用量分析增强：按用户/渠道筛选、用户×模型×渠道三维交叉视图
- 导出总表：一键导出用户×模型×渠道 XLSX 汇总表
- 角色权限优化：admin 选项仅超管可见；admin 不能给自己改角色
- 超管自身修改：超管可修改自己的分组、额度等

**Bug 修复**
- 超管分组列显示 loading...
- 导出报表缺日期参数导致 400
- 日志导出与用量导出共用函数参数混乱

### v3.2.0 (2026-04-16)

**变更**
- 模型映射简化：去掉 JSON/key:value 格式，改为双输入框
- 上游名称隐藏：用户只看到对外模型名
- Token 双模式鉴权：admin 静态 token 和 JWT 均可操作令牌 CRUD

**Bug 修复**
- 前端 api() 非 JSON 响应崩溃
- 渠道测试结果无兜底显示
- Token CRUD 对 admin 静态 token 返回 401

### v3.1.0 (2026-04-15)

**新增**
- 运营角色（operator）：只读查看统计/日志/渠道/用量/报表
- 超级管理员保护：ID=1 不可被修改/删除/降级
- 报表导出：CSV/XLSX，支持明细和汇总，93天限制
- 用量分析：多维度分组统计
- 日志高级过滤：10+ 筛选参数
- 模型映射简化：对外 + 上游双输入框
- 渠道编辑：API Key 脱敏保护

**Bug 修复**
- 渠道编辑 API Key 被脱敏值覆盖
- JWT 角色与数据库不一致
- 用户分组 select 竞态清空 group_id
- 渠道创建路由 422
- 多 admin 用户启动报错

**安全**
- 登录防暴力破解（5次失败锁定5分钟）
- 验证码存储上限
- 导出日期范围限制
- 请求体大小限制
- CORS 从配置读取

### v3.0.0 (2026-04-14)

**架构重构**
- 模块化解耦：Router / Core / Model 三层分离
- 渠道池倒排索引：O(1) 查找替代全表扫描
- 批量日志写入 + 自动清理
- 配额批量合并扣减

## License

MIT
