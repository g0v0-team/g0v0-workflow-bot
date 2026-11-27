# AGENTS.md

本仓库是 `g0v0-team` 内部使用的低流量工作流机器人，基于 **NoneBot2**（FastAPI + aiohttp 驱动）、`nonebot-adapter-github`、`nonebot-adapter-onebot`（OneBot V11）、`nonebot-plugin-orm`（SQLAlchemy Async + Alembic）与 `pydantic-ai` 构建。

所有修改必须遵循**极简、直接、不过度设计**原则。

---

## 1. 核心约束（强制遵守）

### 1.1 代码精简，拒绝过度设计
- 只解决当前明确的需求，不为假想的未来扩展、复用或高并发提前抽象。
- 禁止引入无必要的分层与样板代码：不要写 Repository / DAO、Factory、Strategy、Protocol 包装层，不要写只有一两个方法的包装类，不要为几行到几十行的逻辑新建单独工具文件。
- 没有真正需要维护的可变实例状态时，优先写模块级函数，不要包成 `class`。
- 不要顺手重构任务范围之外的既有代码。

### 1.2 数据库查询尽量内联到 handler
- 简单的查、插、改、删和 upsert 直接在 NoneBot handler 或 HTTP 回调里用 `async with get_session() as session:` 完成。
- 不要为了封装单条或简单几条 SQLAlchemy 查询去写独立的 storage / repository / CRUD 辅助函数或类。
- 只有当同一段非平凡的查询或事务逻辑在多处真实重复出现时，才提取为公共函数。

### 1.3 简单 API 调用直接内联
- 直接在 handler 中调用 `bot.github.rest.*`、`bot.github.async_graphql(...)`、`aiohttp.ClientSession` 或 OneBot API。
- 不要为了几个 REST / GraphQL / HTTP 请求单独封装 `XxxAPI` / `XxxClient` 类，也不要为了中转字段定义多余的 DTO dataclass。

### 1.4 避免为小事新建 service
- **不要默认往 `src/service/` 放代码。**
- 小型或单一职责的功能（指令、Webhook 处理、账号绑定、状态同步、简单通知等）直接写在一个插件模块中，例如 `src/plugins/github/<feature>.py`。
- 只有逻辑确实复杂、有长生命周期状态、或被多个插件复用时（例如基于 Docker 容器的 AI PR Review 引擎），才放到 `src/service/`。

### 1.5 低流量场景，不需要并发处理
- 本项目面向极小团队，事件和指令流量很低，**禁止引入并发工程复杂度**。
- 不要使用 worker pool、任务队列、`asyncio.Lock`、信号量、限流器、内存缓存层或后台 fire-and-forget 调度器（不要使用 `src/service/background_task.py`），除非用户在当前指令中明确要求这样做。
- 多条数据或多次外部请求直接用顺序 `for ...: await ...` 串行处理即可，对于需要并发优化的场景豁免。
- 仅在修改既有 PR Review 队列（`src/service/github/review_queue.py`，因单仓库共享单个 Docker 容器需要串行化）时才保留其队列与锁机制，切勿将该模式复制到新功能。

### 1.6 禁止编写测试
- **不要编写任何测试代码**：不要创建或修改 `tests/` 下的文件，不要补充单元测试/Mock 套件，不要引入 `pytest` 等测试依赖（除非用户在当前指令中明确要求）。
- 使用静态检查（`ruff` 和 `basedpyright`）验证改动。

---

## 2. 项目结构与参考基准

```text
src/
├── plugins/github/
│   ├── __init__.py            # declare nonebot.require(...) 并导入各功能子模块
│   ├── binding.py             # ✅ 推荐参考：配置、handler、DB 查询、HTTP 回调单文件内联
│   ├── pr_review.py           # PR Review 的 GitHub 事件入口
│   └── project_assignment.py  # GitHub Project 同步入口
└── service/
    ├── background_task.py     # 未使用，不要引入
    ├── deepseek_price.py      # 历史遗留单函数文件，不要仿照拆分小文件
    └── github/
        ├── models.py          # ORM Model 与共享 Pydantic 模型
        ├── review*.py / pr_container.py / model_retry.py  # 复杂的 Docker AI Review 子系统（勿作为小功能模板）
        └── project_api.py / project_assignment.py         # 历史偏重分层与加锁写法（勿作为新功能模板）
migrations/versions/           # nonebot-plugin-orm Alembic 迁移脚本
```

- 新增功能时，优先参照 [src/plugins/github/binding.py](src/plugins/github/binding.py) 的紧凑风格：配置、matcher/路由、数据库读写、简单 HTTP/API 调用集中在同一文件内。

---

## 3. 技术栈约定

### NoneBot2 插件与配置
- 插件目录为 `src/plugins`（见 `pyproject.toml`）。在 `src/plugins/github/` 下新增模块后，需在 `src/plugins/github/__init__.py` 中导入，确保 matcher/路由被注册。
- 监听同一 GitHub 事件的多个 matcher 需注意 `block` 参数（例如 `pr_review.py` 使用 `block=True`，共享事件的新 matcher 需使用 `block=False` 避免互相拦截）。
- 插件配置使用 Pydantic `BaseModel` + `nonebot.get_plugin_config(...)`；自定义 HTTP 回调通过 `driver.setup_http_server(...)` 注册（判断 `isinstance(driver, ASGIMixin)`）。

### 数据库与迁移（`nonebot-plugin-orm`）
- ORM `Model` 统一定义在 `src/service/github/models.py`，保证 `nb orm` 能稳定发现所有表结构。
- 数据库需同时兼容 **SQLite**（本地开发）与 **PostgreSQL**（生产环境）。
- QQ 号、GitHub 用户 ID、Installation ID、Comment ID 等数值主键/外键一律使用 `BigInteger`，避免 32 位整型溢出。
- 变更表结构后必须提供对应的 Alembic 迁移脚本（`uv run nb orm revision -m "..."` / `uv run nb orm upgrade`）。

### 代码规范与验证
- Python `>=3.12`，依赖与命令统一通过 `uv` 管理。
- 代码格式与导入排序遵循 `pyproject.toml` 中的 `ruff` 配置（行宽 120；import 分组顺序为 `future` → `standard-library` → `first-party` → `local-folder` → `third-party`）。
- 禁止读取或提交 `.env` / `.env.*` 中的敏感凭据，不要修改 `data/` 下的运行数据。
- 完成代码修改后，运行以下检查确保零报错：

```bash
uv run ruff check src
uv run ruff format --check src
uv run basedpyright
```
