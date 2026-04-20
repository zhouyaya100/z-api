# Zapi 开发文档 (CHANGELOG)

## v3.8.2 (2026-04-18)

### Bug 修复

- **用量分析/我的用量排序**：`group_by=day` 按请求数降序导致日期乱序，趋势图折线乱跳
  - 新增 `order` 参数（默认 `desc`，最新在上；`asc` 按时间升序，图表从左到右）
  - 趋势图请求传 `order=asc`，表格默认 `desc`
  - 其他 group_by 模式不受影响，继续按请求数降序

### 代码清理

- 删除 `reports.py` 重复的 `_to_local` / `_parse_date_filters`，改用 `core/utils.py` 公共函数
- 删除 `settings.py` 重复的 `admin_auth`，使用 `auth.py` 统一版本
- 删除 `stats.py` 未使用的 `_resolve_usernames`（同步函数误用异步 API）
- 修复 `heartbeat.py` 心跳禁用渠道后未同步 `channel_pool` 索引
- 修复 `log_writer.py` `_cleanup_old_logs` 缺少 `timezone` import
- 修复 `main.py` 默认分组 comment 乱码

### 数据清理

- 删除测试渠道 `gemma-4-31B`（ID=2，回环误加，fail_count=67，已禁用）

### 配置恢复

- `min_password_length` 3→8（测试时误改未恢复）
- `rate_limit.rpm` 60→600（测试时误改未恢复）
- `workers` 保持原值 4（Linux 生产环境，Windows 开发用启动命令指定）

---

## v3.8.0 (2026-04-18)

### 架构重构

#### 统一鉴权依赖
- `admin_auth` / `operator_auth` 从各 router 重复定义 → 统一到 `auth.py`，各 router import
- `super_admin_auth` 保留在 `settings.py`（仅设置页使用）
- 认证语义修正：401 = 未认证/无效 token，403 = 认证但权限不足

#### 公共工具模块 `app/core/utils.py`
- 提取 `to_local(dt)` — UTC→本地时间字符串
- 提取 `parse_date_filters(date_from, date_to)` — 本地日期→UTC datetime
- 消除 stats.py / logs.py / reports.py 三处重复定义

#### 重复路由处理器合并
- `logs.py`: `_list_logs_impl` 公共逻辑，admin/operator 路由调用同一实现
- `stats.py`: `_usage_summary_impl` 公共逻辑，admin/operator 路由调用同一实现

#### 授权模型计算统一
- 新增 `app/core/auth_models.py` — `get_group_authed_models()`
- 优先 channel_pool 倒排索引 O(1)，fallback DB 全表扫描
- 消除 list_users / user_dashboard / my_available_models 三处重复

### 新功能

#### 1. 系统设置热更新
- `app/routers/settings.py`: GET/PUT /api/settings 接口
- 22 个可热更新字段 + 5 个只读字段（server/database 需重启）
- YAML 写回策略：`_YAML_MAP` 字段→section.key 映射
- `apply_runtime()` 联动：rate_limiter、error_log_max_entries 实时生效
- 超级管理员（ID=1）专属访问
- 前端独立标签页 "⚙️ 系统设置"，6 色分类卡片

#### 2. 错误日志系统
- `app/core/error_log.py`: MaxEntriesHandler，按条数截断（默认 10000）
- propagate=False 防重复写入，_initialized 防重复初始化
- 记录位置：proxy.py、deductor.py
- 错误日志带 `[request_id]` 前缀，便于追踪
- 两套日志独立策略：数据库日志按天数，错误日志按条数

#### 3. 用量趋势图
- Chart.js CDN 双 Y 轴折线图（请求数+Token量）
- 管理员/用户仪表盘各一
- 数据缓存 + nextTick 重绘 + tab 切回自动重绘

#### 4. 用量分析分页
- 后端 `_build_usage_response` 支持 page/page_size
- 前端"加载更多"按钮，追加模式

#### 5. 渠道池倒排索引
- `app/core/routing/channel_pool.py`: O(1) 模型→渠道查找
- 分组预计算、反向映射、增量更新
- 心跳检测同步 channel_pool 索引

#### 6. 配额批量扣减
- `QuotaDeductor`: CASE WHEN 单条 SQL 批量更新
- 5 秒定时 flush，内存合并队列

### UI 优化

- **设置页**: 卡片式布局，6 色渐变标题栏
- **用户页**: 卡片式，首字母头像，4 列网格，额度进度条
- **指南页**: 卡片+彩色编号，GitHub 暗黑代码高亮
- **仪表盘**: 统计卡片阴影，无限额度紫色标签，Token 进度条
- **确认弹窗**: 通用化 `askDel(msg, action, title, btn, danger)`
- **标签页持久化**: localStorage 存取
- **加载更多**: 统一样式，显示 已加载/总数

### Bug 修复

- **JS falsy 陷阱**: `allowed_models=""` 被 `!variable` 当 false
- **分组绑定渠道**: `allowed_groups=""` 无法表达"无分组"，引入 `__none__`
- **白屏 bug**: 多余 `}}`
- **"无数据"幽灵**: v-else 被插入 div 隔开
- **曲线图不显示**: canvas 未渲染就调 Chart.js → nextTick
- **ref 未暴露**: return{} 中缺少变量
- **分组保存**: updateGroup 缺少 allowed_models 字段
- **图表切回不显示**: 双 nextTick 重试
- **datetime.utcnow() 弃用**: → `datetime.now(timezone.utc).replace(tzinfo=None)`
- **main.py 乱码**: 默认分组 comment 修复

### 性能优化

- N+1 查询消除：stats grouped 批量 IN 查询
- N+1 查询消除：tokens list 批量查用户名
- N+1 查询消除：users list 批量查分组+令牌数
- /v1/models 使用 channel_pool O(1) 查询
- 图表数据缓存

---

## v3.6.0 (2026-04-18)

- 初始代码审查版本
- QuotaDeductor CASE WHEN 批量更新
- JS falsy 陷阱修复
- `__none__` 标记支持

## v3.3.1

- 原始版本
