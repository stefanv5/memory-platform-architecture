# 06 · API 层与服务形态

> 研究对象：`hindsight-api-slim/hindsight_api/`（api/、worker/、main.py、server.py、daemon.py、mcp*.py、metrics*.py、tracing.py、liveness.py、loop_watchdog.py、cancellation.py）、`hindsight-extensions/`、发布包链（hindsight-api-slim → hindsight-api → hindsight-all* → npm 包）。
> 方法：全部论断均直接读码核实，标注 `相对路径:行号`（相对 `hindsight-api-slim/`，其他目录标注全路径）。无法确认的条目在文末单独列出。

---

# 第 1 层【全景】

## 1.1 一句话形态

Hindsight 的服务形态是一个**单体 FastAPI 进程**：`hindsight-api` 一个入口同时承载 REST HTTP 面、MCP 协议面（同进程 ASGI 中间件拦截 `/mcp*`——ASGI：Python 的异步 Web 服务接口标准，应用与中间件都是接收 scope 上下文和 receive/send 两条原始请求/响应字节流通道的可调用对象，中间件即包在应用外的另一层）、以及**默认开启**的后台任务执行器（进程内 `WorkerPoller`，`config.py:1833` `DEFAULT_WORKER_ENABLED = True  # API runs worker by default (standalone mode)`）。数据库（PostgreSQL/Oracle 23ai）既是存储也是任务队列；需要水平扩容时，把 `HINDSIGHT_API_WORKER_ENABLED=false` 关掉 API 内的 poller，另起独立的 `hindsight-worker` 进程，二者通过 `async_operations` 表解耦协作。

三种典型运行形态：

| 形态 | 进程拓扑 | 典型入口 |
|---|---|---|
| 单机内嵌 | 一个 daemon 进程（API+MCP+poller）+ pg0 嵌入式 Postgres | `hindsight-embed` / `hindsight-local-mcp` |
| Docker 单容器 | `hindsight-api` + Next.js 控制面（`start-all.sh` 同容器两个子进程），worker 在 API 进程内 | `docker/standalone/start-all.sh:354`（API 子进程）、`:389`（node 控制面） |
| Kubernetes 分离 | API Deployment（worker 关闭）+ Worker StatefulSet（`command: ["hindsight-worker"]`）+ 控制面 Deployment | `helm/hindsight/templates/worker-statefulset.yaml:41` |

## 1.2 发布包层级：slim 是唯一的"真身"

整条发布链里**只有 `hindsight-api-slim` 装代码**，其余都是依赖伞或生命周期壳：

```mermaid
flowchart LR
    subgraph CORE["真身：hindsight-api-slim（代码包）"]
        SRC["hindsight_api/*<br/>4 个 console scripts"]
    end
    subgraph PYPI["PyPI 伞包 / 捆绑包"]
        HAPI["hindsight-api<br/>deps: slim[all] + 重新声明 4 scripts"]
        HALL["hindsight-all<br/>slim[all] + hindsight-client + hindsight-embed"]
        HSLIM["hindsight-all-slim<br/>slim(无 extras) + client + embed"]
    end
    subgraph NPM["Node 生态"]
        NPMALL["@vectorize-io/hindsight-all<br/>生命周期管理器，无 bin"]
        EMB["hindsight-embed<br/>daemon/profile 管理器"]
    end
    HAPI -->|"依赖"| SRC
    HALL -->|"依赖"| SRC
    HSLIM -->|"依赖"| SRC
    EMB -->|"spawn 本地 API daemon"| SRC
    NPMALL -->|"调用 uvx hindsight-embed"| EMB
```

要点（均已在 pyproject 中核实）：

- **hindsight-api-slim**（`hindsight-api-slim/pyproject.toml:177-181`）声明四个入口，全部落在 `hindsight_api` 包内：
  ```toml
  [project.scripts]
  hindsight-api = "hindsight_api.main:main"
  hindsight-worker = "hindsight_api.worker.main:main"
  hindsight-local-mcp = "hindsight_api.mcp_local:main"
  hindsight-admin = "hindsight_api.admin.cli:main"
  ```
- **hindsight-api**（`hindsight-api/pyproject.toml:13,26`）`dependencies = ["hindsight-api-slim[all]==0.10.1"]`，且 `packages = []` —— 纯依赖伞，唯一作用是给"想要开箱即用（含本地 ML 模型 + pg0）"的用户一个名字；四个 scripts 在它这里重新声明一遍（19-23 行），保持 CLI 命令与包名一致。
- **hindsight-all / hindsight-all-slim**（`hindsight-all/pyproject.toml:12-16`、`hindsight-all-slim/pyproject.toml:12-16`）**不依赖 hindsight-api**，直接拉 `hindsight-api-slim`（all 版带 `[all]` extra，slim 版不带 extras）+ `hindsight-client` + `hindsight-embed`；它们提供的是 Python 库形态的嵌入式用法（`hindsight-all/hindsight/embedded.py` 的 `HindsightEmbedded`，自动拉起本地 daemon）。
- **@vectorize-io/hindsight-all**（`hindsight-all-npm/package.json:2-3`，无 `bin`）是程序化生命周期管理器：`src/command.ts:19-25` 返回 `["uvx", "hindsight-embed@<version>"]`（uvx：uv 生态的 npx——临时运行 PyPI 包的命令），`src/server.ts` 依次执行 profile 创建 → `daemon start` → 轮询 `http://127.0.0.1:8888/health`（30s 预算）。
- **hindsight-embed**（`hindsight-embed/pyproject.toml:18`，入口 `hindsight_embed.cli:main`）负责本地 daemon/profile：数据库默认 `pg0://hindsight-embed-{profile}`，API 默认 8888，控制面端口 = API 端口 + 10000；找 API 命令的顺序是：dev 仓库 `uv run` → 已安装的 `hindsight-api` 可执行文件 → 兜底 `uvx hindsight-api@<version>`（`hindsight_embed/daemon_embed_manager.py:388,433`）。

> 命名注意：`hindsight-api-slim` 的 "slim" 指**不带重量级可选依赖**（torch/本地模型等在 extras 里），而不是功能精简——核心引擎代码 100% 在这个包里。

## 1.3 对外协议面：REST + MCP，同一个进程、同一个 engine

先明确两个贯穿全文的名词：**bank**（记忆库）是 Hindsight 的隔离存储单元，相当于一个 agent 独立的"大脑"，URL 中的 `{bank_id}` 即其标识；**租户（tenant）**是 bank 之上的部署级隔离，映射到独立的数据库 schema。

- **REST**：全部业务路由挂在 `/v1/default/banks/{bank_id}/...` 前缀下（`default` 是租户占位段——`TenantExtension` 决定它映射到哪个 schema），仅两条例外：bank 前缀之外的 `GET /v1/default/chunks/{chunk_id:path}`（7728）与 `GET /v1/default/files/download/{key:path}`（8914）；另有 `/health`、`/health/ready`、`/health/live`、`/metrics`、`/version`、`/v1/bank-template-schema`。扩展 HTTP 路由挂 `/ext/`（http.py:5096）。
- **MCP**：FastMCP 实现的 streamable HTTP/SSE 服务，挂在**同一个 FastAPI app 之外包一层 ASGI 中间件**（`api/__init__.py:97-105`），路径 `/mcp`（多 bank 模式，39 个工具）与 `/mcp/{bank_id}`（单 bank 模式，36 个工具，见 `mcp.py:131-170`）。工具清单在 `mcp_tools.py:36-78` 的 `_ALL_TOOLS`（39 个）：`retain`/`sync_retain`/`recall`/`reflect`、mental-model 全套 CRUD+`refresh`/`clear`、directives、memories/documents/operations/tags 的 list-get-update-delete、bank 管理、knowledge-base 树与页 CRUD。MCP 与 REST 共享同一个 `MemoryEngine` 实例，不是独立服务。
- **根级 `mcp_local.py`**：不是独立 MCP server，而是给本机 Claude Code 用的"一条命令起全家"薄壳——默认 `pg0://hindsight-mcp` 数据库，然后直接调用 `hindsight_api.main.main()`（`mcp_local.py:29-40`），完整 API 跑在 8888，MCP 端点是 `http://localhost:8888/mcp/`（HTTP transport，见其 docstring 1-24 行；`mcp_tools.py` 顶部 docstring 仍称它为 stdio transport，与实际实现不符，属文档残留）。

## 1.4 REST 端点地图（按资源分组）

以下为 `_register_routes`（http.py:5211 起）注册的全部业务路由（省略共同前缀 `/v1/default`；方法后为 handler 名）。这张地图有两个用途：作为后续各小节的公共底图（retain/recall/operations 均会回指），以及开发时按 handler 名/行号直接跳转源码。顺读只需关注四组——**监控/元信息、Banks 与配置、Memory 写入与查询、异步操作管理**——它们覆盖主流程；其余分组按需查阅。

**监控/元信息**
- `GET /health`、`GET /health/ready`（就绪，查数据库，5376/5393）、`GET /health/live`（存活，不碰数据库，5410）、`GET /metrics`（Prometheus，5469）、`GET /version`、`GET /v1/bank-template-schema`

**Banks 与配置**
- `GET /banks`（列表，6293）、`PUT /banks/{bank_id}`（创建或更新，8118，`@audited("create_bank")`）、`PATCH /banks/{bank_id}`（8163）、`DELETE /banks/{bank_id}`（8209）
- `GET /banks/{bank_id}/stats`、`GET /banks/{bank_id}/stats/memories-timeseries`、`GET/PATCH/DELETE /banks/{bank_id}/config`（层级配置，写受 `HINDSIGHT_API_ENABLE_BANK_CONFIG_API` 控制，DELETE 重置 9199）、`POST /banks/{bank_id}/clone`、`GET /banks/{bank_id}/tags`、`POST /banks/{bank_id}/health/llm`（6379，刻意做成 POST——会真实调用一次 provider）
- legacy 的 `GET/PUT /banks/{bank_id}/profile`（8062/8075）与 `POST /banks/{bank_id}/background` 已退役（保留路由恒返 410）

**Memory 写入与查询**
- `POST /banks/{bank_id}/memories`（retain，同步/异步二合一，9573）、`POST .../memories/dry-run-extract`（5633）、`GET .../memories/list`（5525，列表+全文搜索）、`POST .../files/retain`（文件转换后 retain）、`GET /v1/default/files/download/{key:path}`（8914，附件/导出 ZIP 下载）
- `POST .../memories/recall`（检索，路由注册约 5860-5893，handler `api_recall` 在 5886）、`DELETE /banks/{bank_id}/memories`（清空 bank 全部记忆，10028）
- `GET .../memories/{memory_id}`（5753）、`GET .../memories/{memory_id}/history`（5842）、`PATCH .../memories/{memory_id}`（5785，update_memory；**invalidate 也是 PATCH**，body 带 `state: "invalidated"`，请求模型 2683-2696——没有独立 DELETE 单条路由）、`DELETE .../memories/{memory_id}/observations`（9102，清空该记忆的观察）
- `POST /banks/{bank_id}/reflect`（处置感知推理，路由注册 6125 起）

**文档/实体/观察**
- `GET /banks/{bank_id}/documents`、`GET/PATCH/DELETE .../documents/{document_id:path}`（7623/7766/7818）、`GET .../documents/{document_id:path}/chunks`（7525）、`POST .../documents/{document_id:path}/reprocess`（7581）、`GET /banks/{bank_id}/attachments/{attachment_id}`（8860，附件元数据读取）
- `GET /banks/{bank_id}/graph`（5484，bank 级图）、`GET /v1/default/chunks/{chunk_id:path}`（7728，bank 外的 chunk 寻址读）
- `GET /banks/{bank_id}/entities`（6449）、`GET .../entities/graph`（6483）、`GET .../entities/{entity_id}`（6509）、`POST .../entities/{entity_id}/regenerate`（6548）——实体为只读派生数据，无更新/删除端点
- `DELETE /banks/{bank_id}/observations`（清空观察，8983）、`GET /banks/{bank_id}/observations/scopes`（9008）

**Mental Models / Directives / Knowledge Base**
- `GET/POST /banks/{bank_id}/mental-models`、`GET/PATCH/DELETE .../mental-models/{id}`（6636/6872/6915）、`POST .../{id}/refresh`（6753，异步任务）、`POST .../{id}/clear`（6837）、`POST .../{id}/dry-run-refresh`（6787）、`GET .../{id}/history`（6675）
- `GET/POST /banks/{bank_id}/directives`、`GET/PATCH/DELETE .../directives/{directive_id}`（7320/7385/7422）
- `GET .../knowledge-base/tree|search|export`、`POST .../knowledge-base/folders`（6981，创建文件夹）、`POST .../knowledge-base/pages`（7013，创建页面）、`GET .../knowledge-base/pages/{page_id}`（7143）、`PATCH/DELETE .../knowledge-base/nodes/{node_id}`（7171/7240）

**异步操作管理（任务系统的 API 面）**
- `GET /banks/{bank_id}/operations`（7870）、`GET .../operations/{operation_id}`（7910）、`DELETE .../operations/{operation_id}`（取消 pending/processing，7948）、`POST .../operations/{operation_id}/retry`（重排队 failed，7985）、`DELETE .../operations/{operation_id}/delete`（删除终态记录，8015）

**Consolidation / 整库迁移**
- `POST /banks/{bank_id}/consolidate`（手动触发整合）、`POST .../consolidation-strategies/preview`（9042，试跑整合策略）、`POST .../consolidation/recover`
- `GET /banks/{bank_id}/export`（8329，异步导出任务）、`POST .../import`（8242）
- 文档传输：`POST .../document-transfer`（8513，异步导入提交，202 语义）、`POST .../document-transfer/export`（8450，异步导出提交）、`GET .../document-transfer`（8419，已退役 410）；轮询走 `GET .../operations/{operation_id}`，ZIP 经 `GET /v1/default/files/download/{key:path}`（8914）下载——没有 GET 形态的 transfer 端点
- `POST .../transfer/export`（8588）、`POST .../transfer/import`（8672）

**Webhooks / 审计 / LLM 观测 / 提示词**
- `GET/POST /banks/{bank_id}/webhooks`、`PATCH/DELETE .../webhooks/{id}`（无单条 GET；读取靠列表与 deliveries）、`GET .../webhooks/{id}/deliveries`
- `GET /banks/{bank_id}/audit-logs`（+ `GET .../audit-logs/stats`）、`GET /banks/{bank_id}/llm-requests`（+ `GET .../llm-requests/stats`）、`POST /banks/{bank_id}/prompts/preview`

MCP 端能做的操作与上述 REST 面**大体对应**（共享 engine），差异只在工具粒度：MCP 没有 export/import/transfer/consolidate 这类管理型操作；反向地，MCP 的 `list_banks`/`create_bank`/`get_bank`/`get_bank_stats` 直连 engine，没有一一对应的 REST 形态——**没有 `GET /banks/{bank_id}` 单体读取端点**；bank 详情从 `GET /banks` 列表与 `/stats`、`/config` 获取，PATCH/DELETE 只承担写。

---

# 第 2 层【主流程】

## 2.1 服务启动：从 `hindsight-api` 到 uvicorn.run

`hindsight_api/main.py:276` 的 `main()` 按以下顺序装配（`hindsight_api/server.py` 则是给 `uvicorn hindsight_api.server:app` 用途准备的模块级等价物）：

1. `load_dotenv_for_entrypoint()` 加载 .env → `_get_raw_config()` 读配置；
2. 解析 CLI 参数（`_parse_cli_args`，`--host/--port/--workers/--daemon/...`）；
3. **端口预探测**：`_wait_for_port`（main.py:324-327）在昂贵的初始化（pg0 启动、模型加载、迁移）**之前**试绑定端口，避免"初始化 10 秒后才发现端口被占"造成的重启风暴（main.py:139-149 注释引用 #4281）；
4. daemon 模式：`daemonize()`（daemon.py:94-144）用 `subprocess.Popen` + `_HINDSIGHT_DAEMON_CHILD` 环境变量**重 exec 自己**进入后台（不用 double-fork，注释写明原因是 macOS 上 fork 不 exec 会破坏 Apple 框架状态导致 PyTorch/MPS SIGBUS）；
5. 加载两个由环境变量指定的扩展：`OPERATION_VALIDATOR` 与 `TENANT`（main.py:366-377）；
6. 构建 `MemoryEngine` + `create_app`（仅单 worker 模式），多 worker/reload 模式改为传 import string `"hindsight_api.server:app"`，由每个 uvicorn 子进程自行导入（main.py:397-416, 438-439）；
7. `uvicorn.run(...)`（main.py:485）。

main.py 顶部有一段关键的懒加载设计（main.py:45-58）：`create_app`/`MemoryEngine`/扩展机制**不在模块级导入**，通过模块 `__getattr__`（PEP 562）在首次使用时解析：

```python
# hindsight_api/main.py:59-65
_LAZY_IMPORTS: "dict[str, tuple[str, str]]" = {
    "MemoryEngine": (".", "MemoryEngine"),
    "create_app": (".api", "create_app"),
    "OperationValidatorExtension": (".extensions", "OperationValidatorExtension"),
    "TenantExtension": (".extensions", "TenantExtension"),
    "load_extension": (".extensions", "load_extension"),
}
```

逐点讲解：
- 原因是 uvicorn 多进程 supervisor 用 **spawn**：每个子进程会重跑 `sys.argv[0]`（pip console-script 包装器），其首行就是 `from hindsight_api.main import main`——这个模块 import 什么，**每个子进程就都要付一次代价**。注释里给了实测：`.api` 导入约 6.2s、`.extensions` 约 2.6s，把这两者移出模块级后整条命令的导入从 6578ms 降到 312ms（main.py:50-52）。
- 子进程导入若超过 supervisor 的 5s 健康检查会被 SIGKILL 无限重生（这正是被修掉的 respawn bug）。
- 走 `main()` 时通过 `sys.modules[__name__]` 属性访问触发懒导入（main.py:358-363），而不是裸 `from .x import y`——后者会屏蔽测试对模块属性的 patch。

uvicorn 配置里两个值得注意的点（main.py:445-449）：`ws="wsproto"`（避开 websockets 的弃用告警）、`timeout_graceful_shutdown=5`（优雅关闭上限 5s，二次 Ctrl+C 强杀）；`--workers N` 时把 N 写回 `os.environ[ENV_WORKERS]`（main.py:454-459），让每个子进程按 CPU 预算分摊准入限额（见 3.4）。

`server.py`（94 行）是 import-string 模式的目标模块：模块级 `load_dotenv_for_entrypoint()`（server.py:25-27）放在 **import MemoryEngine 之前**——引擎子模块（如 llm_wrapper）在 import 时就会按配置创建信号量，而配置来自环境变量，所以必须先加载 .env，配置才能读到；随后是 profiling 装配（29-40）、扩展加载（58-65）、以及**模块级**构建 `MemoryEngine(run_migrations=config.run_migrations_on_startup)` 与 `app = create_app(...)`（70-87）——注释说明迁移是幂等的，多 worker 各自导入也安全。

### 启动时序（含 FastAPI lifespan）

```mermaid
flowchart TD
    A["hindsight-api CLI<br/>main.py:276"] --> B["load_dotenv + 解析 CLI 参数"]
    B --> C["端口预探测 _wait_for_port<br/>main.py:324"]
    C -->|"--daemon"| D["daemonize 重 exec 后台化<br/>daemon.py:94"]
    D --> E
    C -->|"前台"| E["加载 OPERATION_VALIDATOR / TENANT 扩展<br/>main.py:366-377"]
    E --> F{"--workers 大于 1 ?"}
    F -->|"否"| G["本进程建 MemoryEngine + create_app"]
    F -->|"是"| H["传 import string<br/>每个子进程自建"]
    G --> I["uvicorn.run"]
    H --> I
    I --> J["lifespan 启动<br/>http.py:4870"]
    J --> K["loop_lag 探针 + OpenTelemetry（OTel）指标<br/>http.py:4886-4911"]
    K --> L["initialize_tracing<br/>http.py:4917"]
    L --> M["memory.initialize：建池 + 加载模型 + 跑迁移<br/>（部分维护例程随迁移安装，见 2.5 步骤 1）<br/>http.py:4921"]
    M --> N["loop_watchdog + 进程内 WorkerPoller<br/>http.py:4932-4961"]
    N --> O["tenant/http 扩展 on_startup<br/>http.py:4968-4977"]
    O --> P["开始服务流量"]
```

## 2.2 create_app：一个 app、三层装配

真正的 app 由两段代码拼成：

**第一段（`api/__init__.py:17-109`，统一入口）**：`create_app(memory, http_api_enabled, mcp_api_enabled, mcp_mount_path, initialize_memory)`——先建两个 FastMCP server（多 bank / 单 bank），再调 `api/http.py` 的 `create_http_app` 建 REST app，然后：① 用 `chained_lifespan`（`api/__init__.py:79-92`）把两个 MCP server 的 lifespan 包在 REST app 的 lifespan 外层——启动时先起 MCP、再起 REST，关闭顺序相反（lifespan 即 FastAPI/ASGI 的应用启动/关闭生命周期钩子）；② `app.add_middleware(MCPMiddleware, ...)` 把 MCP 中间件包在最外层（97-105 行）。注释解释不用 Starlette Mount 的原因：Mount 会让无尾斜杠的 `/mcp` 吃 307 重定向。

**第二段（`api/http.py:4832-5118`）**：`create_http_app` 依次做 route_class 替换（`ExcludeNoneRoute`→`UnknownParamsRoute`，5040/5080）、GZip（5050-5052，阈值可配，负值整体关闭）、OpenAPI ValidationError 补丁（5061-5072）、**准入控制器**挂 `app.state.admission`（5084）、注册全部路由（5087）、`/ext/` 扩展路由挂载（5090-5104）、两个纯 ASGI 中间件（见下）、OTel ASGI instrumentation（5116）。

```python
# hindsight_api/api/http.py:5106-5114
    # Client-disconnect cancellation for recall/reflect. Added LAST so it sits
    # OUTSIDE the @app.middleware("http") (BaseHTTPMiddleware) layers above —
    # that placement is mandatory: BaseHTTPMiddleware breaks
    # Request.is_disconnected(), so the only way to observe an abandoned request
    # is to own the raw ASGI receive channel from outside it (issue #2122).
    app.add_middleware(ClientDisconnectCancellationMiddleware)
    # Pure ASGI, so unlike the BaseHTTPMiddleware it replaces it adds no task hop:
    # records the request metrics and attaches X-Ignored-Params for the route class.
    app.add_middleware(HttpObservabilityMiddleware)
```

逐点讲解：
- Starlette 的 `add_middleware` 是插到栈顶（最后添加者在最外层），所以**自外向内**的完整顺序是：`MCPMiddleware` → `HttpObservabilityMiddleware` → `ClientDisconnectCancellationMiddleware` → `GZipMiddleware` → route_class（`UnknownParamsRoute`）→ FastAPI 依赖解析 → handler。上述是 tracing 关闭（默认）时的全部；**tracing 开启时** OTel ASGI 中间件经 `_instrument_app_for_tracing`（http.py:5116-5204）注入，位于 HttpObservability 之外、MCPMiddleware 之内——即 MCP → OTel → Observability → Disconnect → GZip。
- 两个 ASGI 中间件都是纯 ASGI 实现，刻意替换掉了原来的两个 `@app.middleware("http")`（BaseHTTPMiddleware）：注释给出量级——BaseHTTPMiddleware 的每请求子任务 + memory-stream 中转在便宜路由上吃掉约 3 倍吞吐（5074-5079）；`api/observability.py:5-9` 记录 `/health/live` 从约 1500 rps 升到 5100 rps、p99 159ms→33ms。
- `ClientDisconnectCancellationMiddleware` 的位置约束是"必须能读到原始请求字节流（receive 通道）"：任何包装 receive 的层（BaseHTTPMiddleware、OTel 的 receive span）都在它之外才能感知断连。它外面的 `HttpObservabilityMiddleware` 只包 send 不包 receive，OTel 又显式 `exclude_spans=["receive","send"]`（http.py:5203，省 span 的同时避免包装 receive 破坏断连检测），所以最外层是 MCP → observability、断连中间件在第三层也依然有效。

## 2.3 请求 → 中间件 → 依赖 → engine：分层机制图

```mermaid
flowchart TD
    C["Client SDK / curl / MCP 客户端"] -->|"HTTP :8888"| MW1["MCPMiddleware<br/>前缀 /mcp 拦截，否则透传<br/>mcp.py:419"]
    MW1 -->|"REST 路径"| MW2["HttpObservabilityMiddleware<br/>记 hs_asgi_t0 + http 指标<br/>observability.py:70"]
    MW2 --> MW3["ClientDisconnectCancellationMiddleware<br/>recall/reflect 装断连令牌<br/>disconnect.py:49"]
    MW3 --> MW4["GZipMiddleware<br/>阈值 gzip_min_size"]
    MW4 --> ROUTE["route_class UnknownParamsRoute<br/>未知参数收集 + X-Ignored-Params"]
    ROUTE --> DEP["FastAPI 依赖解析<br/>get_request_context → precheck_for → admit_for<br/>http.py:5217/5281/5243"]
    DEP -->|"拒绝: 503/402/401，body 未读"| OUT["返回错误"]
    DEP -->|"放行"| HANDLER["handler: api_retain / api_recall / api_reflect<br/>加 @audited 装饰器"]
    HANDLER --> ENG["MemoryEngine<br/>_authenticate_tenant → _validate_operation → 业务"]
    ENG --> MEMO["MemoriesExtension（存储层插槽，见 3.6）<br/>默认 PostgresMemories"]
    MEMO --> DB[("PostgreSQL / Oracle 23ai")]
    MW1 -->|"/mcp 路径"| FASTMCP["FastMCP multi/single-bank server<br/>contextvars 传 bank_id/租户"]
    MW1 -.-> OTELNOTE["注：tracing 开启时 OTel ASGI 中间件<br/>插在 MCPMiddleware 与 Observability 之间<br/>http.py:5116-5204"]
    FASTMCP --> TOOLS["mcp_tools.py 39 个工具<br/>直接调 engine 方法"]
    TOOLS --> ENG
```

依赖链的三个环节是理解每条重路由的关键（都在 `_register_routes` 内定义）：

1. **`get_request_context`**（http.py:5217-5241）：从 `Authorization` 头取 API key（支持 `Bearer <key>` 与裸 key），并把 `HINDSIGHT_API_EXTENSION_PASSTHROUGH_HEADERS` 白名单中的头收进 `RequestContext.extra_headers`（默认空名单，5240）。第一行 `request.scope.setdefault("hs_deps_t0", time.time())` 是给 recall 的分段计时埋点。
2. **`precheck_for(operation)`**（http.py:5281-5347）：FastAPI **先解析依赖、后反序列化 body**（5248-5250 注释），所以它先 `memory._authenticate_tenant(request_context)`（5319，租户鉴权 + schema 定位），再调 `OperationValidator.precheck`（5337）——扩展可以在这里以 402/429 等拒绝请求，而请求体从未被读进内存。挂在 8 条计费路由上：dry-run-extract、recall、reflect、mental-model 创建/刷新/dry-run-refresh、retain、files/retain（http.py:5651/5891/6145/6720/6765/6811/9602/9895）。
3. **`admit_for(operation)`**（http.py:5243-5279）：yield 风格依赖，`async with controller.admit(...)` 让准入许可覆盖整个请求生命周期；排队期间如果客户端断连（`AdmissionAbandoned`）直接 499 关闭，超时（`AdmissionRejected`）回 503 + `Retry-After`。

## 2.4 贯穿例子 A：一次 POST retain 的完整 HTTP 旅程

请求：`POST /v1/default/banks/my-bank/memories`，body `{"items":[{"content":"..."}],"async":true,"operation_id":"<client uuid>"}`。

1. **中间件段**：MCPMiddleware 前缀不匹配直接透传；HttpObservabilityMiddleware 记 `scope["hs_asgi_t0"]` 并使 `/banks/<id>` 模板化后计入 `hindsight.http.duration`；断连中间件对 `/memories/recall`、`/reflect` 结尾的路径才装令牌（retain 不装）；GZip 视大小压缩请求响应。
2. **依赖段**：`get_request_context` 提取 API key → `precheck_for(RETAIN)`：租户鉴权（`_authenticate_tenant`，把 `_current_schema` ContextVar——请求作用域的上下文变量，可理解为协程版的 thread-local——设为租户 schema）+ validator `precheck`（可 402 拒费）→ `admit_for(RETAIN)` 拿准入许可（默认 32/核并发、排队上限 2s）。
3. **body 解析**：`RetainRequest` 校验（items/document_tags/async_/operation_id...）。
4. **handler `api_retain`**（http.py:9598-9856，`@audited("retain")`）：
   - 多模态内容先规范化：`canonicalize_item_content` 把图片块落成内容寻址附件、正文替换占位符（9639-9647），并处理"文档重发旧占位符"的编辑场景（9619-9637）；附件先入库，失败路径要回收（9664-9666）。
   - items 按 `strategy` 分组，逐条拼 `content_dict`（timestamp→event_date、context、metadata、document_id、entities、tags、observation_scopes、update_mode...，9668-9720）。
   - **async=true 分支**（9722-9752）：每个策略组调一次 `memory.submit_async_retain(...)`，返回 `RetainResponse{success, bank_id, items_count, async: true, operation_id(s)}`。注意：**HTTP 状态是 200**（路由未设 202），异步语义靠 body 里的 `async: true` + `operation_id` 表达，客户端轮询 operations 端点。
   - **async=false 分支**（9753-9801）：若 batch API 开启则强制拒绝同步模式（400，9755-9764）；否则逐组 `retain_batch_async` 同步执行，聚合 token usage 后返回。
5. **错误映射**（9802-9856）：validator 拒绝→其自带 status_code；operation_id 冲突→409；视觉不支持→422；参数错误→400；`MemoryDefenseAllBlockedError`→422 + violations 明细；其余→500（附输入摘要与 traceback）。

`submit_async_retain`（memory_engine.py:21821 起）内部：租户鉴权 → validator `validate_retain` → `sanitize_value` 清洗（防 U+0000/孤立代理对让 jsonb INSERT 直接炸，21870-21884）→ **幂等快路径** → 拒绝重复 document_id（仅异步路径，21904-21920）→ 按 token 预算 `_split_contents_into_async_children` 切子批（21934）→ 总是建 parent operation（21952-21954，"even for single batch - simpler, more reliable code path"）→ 子操作逐个入库 → 通知 task backend。

幂等键机制的完整规则：

```python
# hindsight_api/engine/memory_engine.py:21792-21819（节选）
    async def _resolve_retain_replay(self, operation_id: uuid.UUID, bank_id: str) -> dict[str, Any] | None:
        """Resolve a caller-supplied async retain operation_id to a prior submission.

        Returns the replay response when the id is this bank's own batch_retain
        parent (a retried submission after a lost acknowledgement — no new work),
        ``None`` when the id is unused (free to create), and raises
        RetainOperationConflictError when the id is already used by a different
        bank or a different operation type.
        """
```

逐点讲解：
- 客户端可自选 UUID 作 `operation_id`；重发同一 id 时若该 id 已是**本 bank 的 batch_retain parent**，直接回放原响应（`items_count` 存在 parent 的 `result_metadata` 里），**不产生新工作**——这是"丢失应答后重试不重复入库"的保证（21837-21841 docstring：parent 主键本身就是并发权威，无需额外去重列）。
- id 被其他 bank 或其他操作类型占用 → `RetainOperationConflictError` → HTTP 409。
- 该 SELECT 刻意不在创建事务里（21889-21897 注释）：并发首次提交靠主键冲突兜底，快路径只为常见的顺序重试省事。
- 其他异步操作（consolidate、refresh_mental_model、export/import 等）没有客户端幂等键，靠的是**提交期去重**（`dedupe_by_bank` / `dedupe_in_flight_payload_key`，见 3.1）。

## 2.5 贯穿例子 B：worker 认领一次 retain 任务的数据库操作全程

前置：API 已把任务写入队列。worker 侧（`worker/main.py:165` 的 `main()`，或 API 进程 lifespan 里的同款 `WorkerPoller`）以 `poll_interval_ms`（默认 500ms，config.py:1835）循环执行。

**步骤 1 — 找有活的 schema**（多租户时）：`_scan_active_schemas`（poller.py:410-446）优先调用服务端例程 `SELECT * FROM public.schemas_with_pending_work()`（443 行，一次往返替代 N 次逐 schema EXISTS）。注意这个例程**不由 Hindsight 安装**——`engine/db/optional_routines.py` docstring 明言 API 与迁移都不装、由运维带外安装（如 Helm hook）；未安装时回退 457-459 的 per-schema `EXISTS`（单 schema 部署即如此）。

**步骤 2 — 计算本进程可用槽位**：`_get_available_slots`（poller.py:466-496）。`max_slots` 默认 10（config.py:1839），`slot_reservations` 默认 `{"consolidation": 2}` —— 预留池保底、剩余进共享池。

**步骤 3 — 声明式认领**（`claim_batch` → `_claim_batch_for_schema_inner`，poller.py:524-728）：整轮复用一条池化连接（563 行注释：每次 acquire/release 都有 session GUC + RESET ALL 仪式，逐 schema 取连接会把 2 条有效查询放大成约 12 条语句，#3499），但每个 schema 的认领**各自开事务**（688 行），行锁只到本 schema 认领提交为止。认领 SQL 由 `backend.ops.claim_tasks` 生成（ops_postgresql.py:1825-1898），核心是 `FOR UPDATE SKIP LOCKED`（已被其他 worker 锁住的行直接跳过、不等待，所以多 worker 并发认领互不阻塞）：先按预留池逐类型认领（consolidation 走优先级分层的 `_claim_consolidation_tasks`），再认领共享池。

示例（字段名与代码一致）：`async_operations` 中一行。`serialization_key` 是认领层的串行化分组键：同组作业不并发（异步 retain 按文档 id 填入，consolidation 按 bank 填入），折叠也按它找兄弟。
```
operation_id=7f3c...  bank_id='my-bank'  operation_type='retain'
status='pending'  task_payload={"type":"batch_retain","operation_id":"7f3c...","bank_id":"my-bank",
              "contents":[{"content":"..."}],"_traceparent":"00-4bf9..."}
serialization_key='doc-42'  retry_count=0  next_retry_at=NULL
```
共享池认领 SQL（ops_postgresql.py:1794-1823，rot/fifo 两 CTE 一条语句）：

```sql
WITH rot AS (                       -- 轮转层：bank_id 严格大于游标的第一行
    SELECT o.operation_id, 0 AS tier, o.created_at
    FROM async_operations o
    WHERE o.status='pending' AND o.task_payload IS NOT NULL
      AND o.operation_type != 'consolidation'
      AND (o.next_retry_at IS NULL OR o.next_retry_at <= NOW())
      AND {bank_serialization_sql} AND {document_serialization_sql}
      AND o.bank_id > $1            -- $1 = 上轮服务到的 bank 游标
    ORDER BY o.bank_id LIMIT 1
), fifo AS (                        -- FIFO 层：其余按 created_at 补满
    SELECT o.operation_id, 1 AS tier, o.created_at
    FROM async_operations o
    WHERE <同上谓词> AND NOT EXISTS (SELECT 1 FROM rot WHERE rot.operation_id = o.operation_id)
    ORDER BY o.created_at LIMIT $2
), cand AS (                        -- 两层合并（轮转行优先）
    SELECT * FROM rot UNION ALL SELECT * FROM fifo
)
SELECT ... FROM cand c JOIN async_operations o ON o.operation_id = c.operation_id
WHERE <谓词再次成立>                 -- FOR UPDATE 下重查，防双认领
ORDER BY c.tier, c.created_at LIMIT $2
FOR UPDATE OF o SKIP LOCKED
```

随后（仍在 claim 事务内）：`_fold_retain_peers`（poller.py:730-826）把同一 `serialization_key`（同一文档）排队中的兄弟 retain **折叠**进本次执行——`fetch_foldable_retain_peers`（ops_postgresql.py:1919-1946，同样 `FOR UPDATE SKIP LOCKED`）取 peers，`plan_retain_fold` 按 token 预算规划，失败则退回单任务执行。落账分两次：主行在 `claim_tasks` 末尾统一 `mark_operations_processing`（ops_postgresql.py:1894-1896），折叠并入的 peers 由 `_fold_retain_peers` 再补一次 mark（poller.py:806）。落账语句：

```sql
UPDATE async_operations
SET status = 'processing', worker_id = $1, claimed_at = now(), updated_at = now()
WHERE operation_id = ANY($2)
```

**步骤 4 — 执行**：poller 把行包成 `ClaimedTask`（payload 反 JSON、注入 `_retry_count/_operation_id`、DB 权威的 `operation_type`，poller.py:708-718），`asyncio.create_task` 火后不管（1055-1086），经 `_run_executor` 的 wall-clock 上限包装（retain 绝对上限、consolidation 是"无进展才计时"的空闲上限，poller.py:79-89, 1100-1142）调 `MemoryEngine.execute_task`。engine 侧：恢复 payload 里的 traceparent 接回原 trace（memory_engine.py:3945-3961）、设 `_schema` ContextVar（3969-3971）、**先查行状态是否已被取消**（3973-3985），然后按 `type` 分发到 `_handle_batch_retain` 等处理器（3999-4008）。

**步骤 5 — 终态**：成功 → `_mark_completed`（poller.py:857-871，`WHERE ... AND status='processing'` 防覆盖）；失败 → `_mark_failed`（873-899，`WHERE status <> 'cancelled'` 防把取消改成失败）；二者都在事务内做 batch_retain 父子聚合 `_maybe_update_parent_operation`（901-1004：FOR UPDATE 锁父行，全部子件到达终态时父行置 completed/failed/cancelled，failed 优先且继承最高频子错误）。

**步骤 6 — 客户端收尾**：`GET /banks/{bank_id}/operations/{operation_id}` 轮询（7870/7919）；失败后可 `POST .../retry` 重排队（7985），pending/processing 可 `DELETE .../{operation_id}` 协作式取消（7947：pending 永不启动，processing 由执行中任务在下个检查点停下）。

### 任务状态机

```mermaid
stateDiagram-v2
    [*] --> pending: INSERT，payload 原子携带
    pending --> processing: 认领 + mark_operations_processing
    processing --> completed: mark_completed
    processing --> failed: mark_failed，guard 排除 cancelled
    processing --> pending: schedule_retry，retry_count 加一
    processing --> pending: defer，不计 retry
    processing --> cancelled: DELETE operations 操作
    pending --> cancelled: 取消/清理（见 3.3）
    failed --> pending: POST operations 重试
    completed --> [*]: 清理（见 3.3，默认关）
```

### 提交 → 认领 → 执行时序

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API 进程
    participant D as PostgreSQL
    participant W as WorkerPoller
    C->>A: POST memories，async=true
    A->>A: 鉴权+准入+validator
    A->>D: _ensure_bank_exists 存在性探测，必要时同事务建 bank
    A->>D: INSERT parent 行（无 payload）+ 逐子行 INSERT（payload+serialization_key）
    A-->>C: 200 + operation_id
    loop 每 500ms
        W->>D: schemas_with_pending_work 或逐 schema EXISTS
        W->>D: claim_tasks: FOR UPDATE SKIP LOCKED
        D-->>W: 行集，含 task_payload
        W->>D: mark_operations_processing
        W->>W: 折叠同文档兄弟任务，火后不管执行
    end
    W->>D: 业务写入 documents、memory_units、links
    W->>D: UPDATE status=completed 或 failed 或 pending+next_retry_at
    C->>A: GET operations，operation_id 轮询
    A-->>C: 状态与 result_metadata
```

---

# 第 3 层【机制深潜】

## 3.1 任务队列：`async_operations` 就是队列

任务队列的三个核心设计在代码中逐一坐实：`async_operations` 表就是队列（下文表结构）；三种 task backend 分的是"提交侧如何通知"的工（下表）；认领用 `FOR UPDATE SKIP LOCKED` SQL（3.2 展开）。

**表结构**：初始迁移建表（`alembic/versions/5a366d414dce_initial_schema.py:217-243`）只有 `operation_id(UUID PK)/bank_id/operation_type/status/created_at/updated_at/completed_at/error_message/result_metadata(jsonb)`，status CHECK 约束限定 `('pending','processing','completed','failed')`。任务化所需的列由后续迁移分批补齐：`l7g8h9i0j1k2_add_worker_columns.py:40-69` 加 `worker_id/claimed_at/retry_count/task_payload(jsonb)` 四列（并建 `worker_id` 部分索引，79 行附近）；`next_retry_at` 由 `e4f5a6b7c8d9_add_webhooks_tables.py:49-55` 添加（连同 `(status, next_retry_at)` 轮询索引）；`d9c1a7b4e2f6_async_operations_serialization_key.py:54-61` 加 `serialization_key` 与 `(bank_id, serialization_key)` 的**部分（非唯一）索引**——`CREATE INDEX`，WHERE 还含 `serialization_key IS NOT NULL AND status IN ('pending','processing')`。同文档 retain 的串行化保证来自认领谓词 `bank_serialization_sql`（见 3.2）而非数据库唯一约束，这个索引只是让谓词查得快；折叠（3.3 之前例 B）同样按它定位兄弟行。（`cancelled` 状态由代码写入，CHECK 约束在 `i4j5k6l7m8n9_add_cancelled_status_to_async_operations.py:28-31` 扩为五值；api-slim 迁移目录里另有 `add_scheduled_mental_model_refresh_routine`、`schemas_with_expired_operations` 等服务端例程迁移。）

**operation_type 清单**（代码中出现者）：`retain`（= batch_retain 子件，payload 的 `type` 字段为 `"batch_retain"`，`memory_engine.py:22051-22056`）、`batch_retain`（父聚合行，payload 为 NULL 不可认领）、`file_convert_retain`、`consolidation`、`refresh_mental_model`、`webhook_delivery`、`import_documents`/`export_documents`/`import_bank`/`export_bank`、`clone_bank`（memory_engine.py:7068）、`graph_maintenance`、`vector_index_maintenance`。

**三种 task backend**（`engine/task_backend.py`）——注意它们的分工是"提交侧如何通知"，不是三套队列：

| backend | 类位置 | `submit_task` 行为 | 使用者 |
|---|---|---|---|
| `BrokerTaskBackend` | task_backend.py:153 | 对已有行补写 `task_payload`（`WHERE ... AND task_payload IS NULL` 防覆盖，228-238；兼容路径，见下）；无 operation_id 时自行 INSERT（243-255） | API 进程默认（memory_engine.py:2661-2667） |
| `WorkerTaskBackend` | task_backend.py:126-150 | **no-op**："row already exists in async_operations; a worker will claim it" | `hindsight-worker` 独立进程（worker/main.py:284-289）——worker 执行中触发的子任务（如 retain 触发的 consolidation）行已入库，交给下一轮轮询，避免阻塞父任务 |
| `SyncTaskBackend` | task_backend.py:95-123 | 立即内联执行（`_execute_task`） | 测试/嵌入用法 |

关键演进点（memory_engine.py:21584-21587 注释）：早期 INSERT 不带 payload、靠 `submit_task` 二次 UPDATE 补——两次写之间崩溃会留下"payload 为 NULL 的行"，而认领查询要求 `task_payload IS NOT NULL`，该行永远无人认领。现在 payload 与行**同一 INSERT 原子写入**（21767-21778），UPDATE 只作兼容。

**提交期去重**（`_submit_async_operation`，memory_engine.py:21535-21790）：retain 之外的异步提交都走这个通用入口，两级去重避免重复任务堆积——`dedupe_by_bank`（21557-21573）按 bank+operation_type 查 pending（可选含 processing）行，命中则复用既有 operation_id 直接返回 `deduplicated=True`，适用于"一个 bank 同时只该有一个"的作业（如手动 consolidate）；`dedupe_in_flight_payload_key`（21714-21766）更细，按 `task_payload->>'<key>'` 匹配同一负载主体（如同一 `mental_model_id`）的在途任务。去重开启时提交会先对 banks 行加 `FOR NO KEY UPDATE` 锁（21599-21632），把"查重→插入"串行化，防两个并发提交都看不到对方（#1842）。

## 3.2 认领 SQL 的核心逻辑与公平性

`claim_tasks`（ops_postgresql.py:1825-1898）两阶段：Phase 1 按预留池逐类型认领（consolidation 有专属路径与 bank 优先级分层，`_claim_consolidation_tasks`，ops_postgresql.py:1495-1575）；Phase 2 认领共享池——2a 非 consolidation 走 rot/fifo 一条语句，2b consolidation 补位。认领完统一 `mark_operations_processing`（:1900-1917）。

**公平性是三层旋转的叠加**：

1. **租户层轮转**（poller.py:566-652）：pass 1 只扫"有活"的 schema、每个池每 schema 至多认领 1 条；pass 2 才向有活的 schema 回填剩余槽位。`_next_schema_idx` 指针在轮末推进到"最后服务的 schema 之后"，让忙租户不能永远排在队首。
2. **bank 层轮转**（rot/fifo，ops_postgresql.py:1730-1776 `_claim_shared_tasks` docstring；`_claim_reserved_tasks` 在 1702）：全局 FIFO 会让一个正在批量灌入库的 bank 占满所有槽、别的 bank 排队（#3861）。`rot` CTE 是"bank_id 严格大于游标的第一行"（tier 0），`fifo` 兜底补满（tier 1）；游标 `_next_bank_cursor` 从结果里免费带回（claim_tasks:1876），空 rot 层即游标到头自动重开。单 bank 部署只多付一次空 seek。注释给出实测：5 万 pending 行时 rot seek 0.10ms vs 裸 FIFO 0.02ms，且轮转与 FIFO 合并成一条语句、无额外往返。
3. **bank 串行化谓词**（`bank_serialization_sql`，engine/db/ops.py:147-192）：每个查询都带这个谓词，保证**同一 bank 的同类作业（consolidation/graph_maintenance）全局至多一个 in-flight**——两个并发 consolidation 会读到同一批未整合记忆并重复喂给 LLM（#3700）。谓词有两个分支：`processing` 分支禁止与在跑的作业并发；"严格更老的 pending"分支防止一个批次的认领把同 bank 的多行全拿走（含"对端已认领未提交"的窗口）。它是**谓词而非独立认领阶段**，理由写在注释里：graph_maintenance 没有预留槽（默认 0），fairness pass 只给 `shared_limit=1`，若做成独立阶段会被一条排队的 retain 无限期饿死。

**重查为什么必要**：外层查询在 `FOR UPDATE` 下重申全部谓词（1760-1763 注释）——CTE 读行时未加锁，两个 worker 并发时，被对端先锁住的行会在重查时落选而不是被认领两次。代价是 SKIP LOCKED 偶尔让本轮少拿几行，下一轮自愈。

## 3.3 失败恢复与重试

执行期的五条出路（`_execute_task_inner`，poller.py:1196-1259）：

| 异常 | 处理 | 写库效果 |
|---|---|---|
| 正常完成 | `_mark_all_completed`（fold 的全部 operation_id 一起终态） | `status='completed'` |
| `_WallTimeoutExceeded` | 失败 + 引擎回调 `on_task_wall_timeout`（补发 consolidation 失败 webhook） | `status='failed'` + 可读原因（含 stage 与 env 变量名） |
| `DeferOperation` | `_defer_operation`（1032-1053） | 回 pending + `next_retry_at`，**不**加 retry_count、不写 error_message（"intentional backpressure, not a failure"） |
| `RetryTaskAt` | `_schedule_retry`（1006-1030） | 回 pending + `next_retry_at` + `retry_count+1` + `worker_id=NULL, claimed_at=NULL` |
| 其他异常 | 先判 `is_store_backpressure`（1230-1239，存储端因索引落后而甩负载→defer，不烧重试预算）；否则 `_mark_all_failed`，若连失败写库本身也失败（池耗尽等）则 `_reclaim_own_processing_tasks` 单行抢救（1244-1258） | `status='failed'` 或回 pending |

**崩溃恢复的三条线**（全部收口在 `_reclaim_own_processing_tasks`，poller.py:1271-1351）：

- 启动时 `recover_own_tasks`（1353-1395）：把**本 worker_id**名下 `processing` 的行——低于重试上限的回 pending（`retry_count+1`）、达到上限的置 failed（防"认领→磨死→回收"死循环，#2675/#2834）——外加 batch API 行恢复（`_recover_batch_operations`，1432-1491，按 `result_metadata->>'batch_id'` 识别）与孤儿 parent 收敛（`_reconcile_orphaned_parents`，1501+；**孤儿 parent** 指 batch_retain 父行在子件已全部终态后仍卡在 pending/processing——崩溃发生在子件终态提交与父行更新之间，或子件压根没提交，状态图里 `pending --> cancelled` 的"清理"即指此收敛）。
- 关停时 `release_own_tasks`（1397-1430）：drain 超时后把仍归自己的行还回 pending。动机写在注释里：默认 worker_id 派生自 hostname，容器里**永不复现**，没有这条路径那些行会永远卡在 processing（#3228）。
- 运行中兜底：终态写库失败时按 operation_id 单行抢救（1244-1258）。

**wall-clock 上限**（poller.py:79-89）：retain 类是**绝对**上限（一个文档跑一小时必有蹊跷）；consolidation 是**空闲**上限——每个提交批次推进 stage 都会 `asyncio.timeout(...).reschedule()` 重置时钟（1100-1142），只有真正停滞才触发。触发时取消执行器，因此引擎自己的 except 块（会发 webhook）不会跑，poller 用 `on_wall_timeout` 回调代为通知（1144-1157）。设计动机：卡死的任务会永占 worker 槽、操作停在 API 既不重试也不可取消的 `processing`（#3002）。

**worker 与 API 的关系总结**：同一张队列表、同一份认领代码（`WorkerPoller`）。默认 API 进程内嵌一个 poller（standalone）；`hindsight-worker` 是同构的独立进程，区别仅在 task backend 用 no-op 的 `WorkerTaskBackend`、且 `run_migrations=False`（worker/main.py:284-289），启动前会检查 `supports_worker_poller`（301-304，不支持的 backend 直接退出而非悄悄同步执行）。`hindsight-admin` 另有 `decommission-worker(s)`/`worker-status` 命令按 worker_id 清理遗留行（admin/cli.py:1352-1510 附近）。

## 3.4 admission 准入控制：限"等待"而非限"并发"

`api/admission.py` 的 docstring 把问题定义得很清楚：裸信号量是背压不是准入——2 vCPU 容器上 1024 并发 recall 撞 32 许可的信号量，p50 变 12.8s，**延迟没有消失，只是从事件循环搬进了信号量队列**，服务器还在为早已放弃的客户端算响应（admission.py:5-9）。所以每个 lane——车道：每类高成本操作一条独立的"并发 + 排队"通道——只有两个数：`max_in_flight`（并发）+ `max_wait_seconds`（**排队上限**），超时立即 503 + `Retry-After`。

```python
# hindsight_api/api/admission.py:259-275
def build_controller_from_config(config) -> AdmissionController:
    """... Only the three high-volume operations get a lane. ...
    A lane resolving to 0 in-flight is off (set its env var negative)."""
    return AdmissionController(
        {
            "recall": LaneConfig(config.admission_recall_max_in_flight, config.admission_recall_max_wait_seconds),
            "reflect": LaneConfig(config.admission_reflect_max_in_flight, config.admission_reflect_max_wait_seconds),
            "retain": LaneConfig(config.admission_retain_max_in_flight, config.admission_retain_max_wait_seconds),
        }
    )
```

逐点讲解：
- **什么条件下拒绝**：lane 启用（`max_in_flight > 0`）且请求在队列里等了超过 `max_wait_seconds` → `AdmissionRejected` → handler 依赖层转 **503 + `Retry-After`**（http.py:5272-5277）。默认值（config.py:1550-1571）：recall 16/核、等 30s；reflect 16/核、等 5s；retain 32/核、等 2s。`max_in_flight` 环境变量为**负数**才关闭 lane（0 表示"按核数推导"）；`max_wait_seconds=0` 是"绝不排队"模式——内部仍走 acquire，只是 deadline 设成 0.001s（admission.py:215），继续走同一条 acquire 路径，统计不失真。
- **等待是可中断的**：`_acquire_unless_abandoned`（109-152）用 `asyncio.wait` 让"拿许可"与"断连令牌"赛跑；客户端先走则抛 `_ClientGone` → **499** 关闭（http.py:5268-5271），既不占槽也不造响应。取消 pending acquire 后若许可恰好已到手，会立刻 `semaphore.release()` 归还防泄漏（142-148）。
- **为什么按操作分 lane 而不是全局限流**：同一 API 上操作成本跨三个量级（/health/live 约 0.1ms、bank stats 约 0.5ms、recall 约 23ms，admission.py:26-31）——按 recall 校准的全局限流会掐死健康检查。
- **限额是每 worker 进程一份**（admission.py:33-35）：`--workers N` 时全局预算 = N × max_in_flight。main.py:454-459 把 N 写回环境变量，让子进程各自分摊 CPU 预算。
- 拒绝点在 FastAPI 依赖里，**body 反序列化之前**——最便宜的拒绝位置（http.py:5248-5250）。

## 3.5 可观测性与运行时安全

**metrics（metrics.py，1351 行）**：OpenTelemetry API + Prometheus reader，`/metrics` 暴露。核心 instruments（metrics.py:438-650）：`hindsight.operation.duration/.total`（取消的请求**不计入** total——success/failure 都不算，issue #2122，metrics.py:45-68 沿 `__cause__` 链识别 `OperationCancelledError`）、`hindsight.llm.duration/tokens.*`、`hindsight.http.duration/.requests.total/.in_progress`、`hindsight.db.pool.acquire_wait` + size/idle/min/max/**waiting**（waiting 需要池侧 instrumentation 提供 asyncpg 不暴露的等待者计数）、recall/retain/validator 分阶段直方图、`hindsight.event_loop.stalls/stall_duration/lag`。基数控制：路径模板化（`normalize_http_endpoint`，149）、bank_id/tenant 标签默认关闭、token 桶化。可选 backlog gauge（默认关，`HINDSIGHT_API_METRICS_BACKLOG_ENABLED`）由 30s 后台循环跨所有 schema COUNT `async_operations`（1116 起）。

**多 worker 指标（metrics_multiworker.py）**：`--workers N` 时一次抓取只会打到随机一个 worker。方案不是 OTel 多进程模式（PrometheusMetricReader 跨进程不可合并），而是：每个 worker 用非阻塞 `flock` 认领槽位 0..N-1（内核在进程死亡时自动放锁，重启 worker 复用槽号、标签有界）→ 守护线程每 5s 原子写 `worker-<slot>.prom` 快照到以 supervisor pid 命名的临时目录 → `/metrics` 把自己 + 15s 内新鲜的其他槽快照合并，所有样本贴 `api_worker="<slot>"` 标签，**不求和**（"summing is a query-time decision"，metrics_multiworker.py:96-98）。开关 `HINDSIGHT_API_METRICS_WORKER_LABEL`（默认 false）。

**tracing（tracing.py）**：OTLP HTTP exporter，`HINDSIGHT_API_OTEL_TRACES_ENABLED` + `HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT`（默认均关）。span 层级：HTTP server span 按操作**改名**（`hindsight.recall/retain/reflect/...`，http.py:5125-5162，让 trace 标题是操作而非 URL 模板）→ engine 的 `hindsight.*` 父 span → GenAI 语义规范的 LLM 子 span（prompt/completion 以事件附带，内容截断 10 万字符）→ reflect 的 per-tool span。跨进程续 trace：入队时把 W3C traceparent 注进 payload 的 `_traceparent` 键，worker 的 `execute_task` 解出并 `otel_context.attach`（memory_engine.py:3945-3961）。

**liveness vs readiness（liveness.py + 路由）**：`/health/live` 只回答"进程是否 wedged"，**永不触库**（模块 docstring：一个 `SELECT 1` 存活探针会把数据库退化放大成全量重启风暴——所有 pod 同时被杀，已认领任务集体重新入队冲向失败悬崖）；`/health`/`/health/ready` 查 `memory.health_check()`，不健康回 503，用于**挡流量**而非重启进程。worker 侧同款三分法，外加 `seconds_since_last_poll` 仅供告警、永不影响状态码。

**loop_watchdog（独立线程）**：worker 和 API 共用一个事件循环，同步调用卡住循环时 `/health` 无法调度。看门狗运行在**另一个 OS 线程**（"基于协程的监视器会被它想观察的卡顿本身冻住"），每 250ms `loop.call_soon_threadsafe` 投一个 ping，1s 内未被服务就 `sys._current_frames()` 抓循环线程栈打 `[EVENT LOOP BLOCKED]` 告警并计 `hindsight.event_loop.stalls`——只观察、不动循环，uvloop 兼容（loop_watchdog.py:9-16, 107-163）。

**loop_lag（进程内探针）**：每 50ms tick 测"睡过头"多少，采样进 `hindsight.event_loop.lag` 直方图并按窗口打 p50/p90/p99 日志；动机：分阶段计时器只覆盖自己 await 的部分，"可运行却没在跑"的协程不可见（实测分阶段计时只覆盖 recall 墙钟的 10%，loop_lag.py:1-13）。默认关闭。

**cancellation（三跳传播，cancellation.py + api/disconnect.py）**：① `ClientDisconnectCancellationMiddleware`（纯 ASGI，装在 BaseHTTPMiddleware 之外，因为后者的内存流会让 `is_disconnected()` 永远不触发，#2122）只在 `/memories/recall`、`/reflect` 上监听原始 `receive` 的 `http.disconnect`；② 令牌挂 `scope["hindsight.cancellation_token"]`，handler 经 `run_cancellable_on_disconnect` 注入 `RequestContext.cancellation`，engine 在**阶段边界**轮询 `raise_if_cancelled()`（已进入 executor 线程的图扩展/重排计算无法被取消——协作式设计的边界）；③ 捕获 `OperationCancelledError` 转 HTTP 499。`OperationCancelledError` 刻意继承 `Exception` 而非 BaseException——引擎里大量 `except Exception` 需要显式重抛它，且 `gather(return_exceptions=True)` 的结果分类依赖 isinstance（cancellation.py:25-40）。

**profiling 与 http_probe**：`HINDSIGHT_API_PROFILE`（JSON）驱动进程级 cProfile 周期性把**窗口增量**打进日志流（理由：`kubectl logs --previous` 比 file 活得久）；`http_probe.py` 是 stdlib-only 的探活小工具（禁止 import 引擎/三方包，有测试守卫），替换掉运行时镜像里的 curl（消 9 个 HIGH CVE）。

## 3.6 扩展槽位：6 个插口、一条加载协议

加载协议（`hindsight_api/extensions/loader.py:24-125`）：读 `HINDSIGHT_API_{PREFIX}_EXTENSION` 环境变量 → 值为 `"module.path:ClassName"` → importlib 导入并校验 `issubclass(base_class)` → 把**所有** `HINDSIGHT_API_{PREFIX}_*` 环境变量（去掉 `_EXTENSION` 本身）剥前缀小写成 config dict → 实例化并 `set_context`。加载点分布：TENANT/OPERATION_VALIDATOR 在四个入口各自加载（main.py:366-377、server.py:58-65、worker/main.py:271-275、admin/cli.py:304）；HTTP 在 create_http_app（http.py:4864-4867）；MCP 在 `create_mcp_server`（mcp.py:199-202）；MEMORY_DEFENSE 与 MEMORIES 由 engine 自己加载兜底（memory_engine.py:2775-2781、engine/memories/__init__.py:43-54）。

| 槽位 | 抽象类（file:line） | 钩子 | 官方实现 |
|---|---|---|---|
| TENANT | `TenantExtension`（extensions/tenant.py:54） | `authenticate`（返回 TenantContext.schema_name）、`authenticate_mcp`、`list_tenants`（worker 轮询/迁移扇出）、`get_tenant_config`/`get_allowed_config_fields`（层级配置与写权限）、`extra_bank_tables`/`provision_bank_tables` | 内置 `DefaultTenantExtension`（无鉴权）、`ApiKeyTenantExtension`（单共享密钥）；registry 包 `static-keys-tenant`（env 声明 user:key 对→每用户 schema）、`supabase-tenant`（JWKS 验 JWT） |
| OPERATION_VALIDATOR | `OperationValidatorExtension`（extensions/operation_validator.py:553） | `precheck`（8 条计费路由、body 未读）；`validate_retain/recall/reflect/create_bank`；`validate_bank_read`/`validate_bank_write`（各 38 个调用点覆盖 30/35 个操作名）；`on_*_complete`（ret/recall/reflect/... 完成回调，计费计量面）；`filter_bank_list`/`filter_mcp_tools`（只减不增） | 无内置实现（云版计量/配额面）；`validate_consolidate`/`on_consolidate_complete` 在 api-slim 内无调用点 |
| HTTP | `HttpExtension`（extensions/http.py:19） | `get_router`（挂 `/ext`）、`get_root_router`（挂根）、生命周期钩子 | 无 |
| MCP | `MCPExtension`（extensions/mcp.py:21） | `register_tools(mcp, memory)` 追加工具 | 无 |
| MEMORY_DEFENSE | `MemoryDefenseExtension`（extensions/memory_defense.py:336） | `screen(policy, bank_id, document_id, content, tags)` → allow/redact/block | 内置 `MemoryDefenseRegexExtension`（约 40 模式的密钥/PII 目录 + Luhn 卡号校验） |
| MEMORIES | `MemoriesExtension`（engine/memories/base.py:770） | 整个存储+检索面（写入路径、recall 各臂、寻址读、维护），`store_owned` 极性标志 | 默认 `PostgresMemories` |

实例化后的全程装配：`MemoryEngine.__init__`（memory_engine.py:2729-2781）给 validator 套计时 instrumentation（validator_instrumentation.py:57，逐钩子记 `hindsight.validator.phase.duration`）、给 tenant/validator 补 `set_context`（它们在 engine 之前构造）、构建 `DefaultExtensionContext`（给扩展受控的 `run_migration`/`get_memory_engine` API；基类 `ExtensionContext` 在 context.py:11，默认实现 `DefaultExtensionContext` 在 context.py:76）。memory defense 在 retain 编排器里逐内容项执行（engine/retain/orchestrator.py:1465-1514）：REDACT 会重写内容对象与原始 dict（落库前），BLOCK 累积违规，全阻断 → `MemoryDefenseAllBlockedError` → HTTP 422。

## 3.7 bank 隔离在 API 层的体现

- **HTTP 面**：所有业务路由的 `{bank_id}` 是显式路径参数；请求进入 engine 前第一个引擎级动作是 `_authenticate_tenant`（memory_engine.py:2855-2891）：`request_context.internal`（后台/worker 任务）与 `mcp_authenticated`（传输层已验）直接复用当前 schema，否则调 `tenant_extension.authenticate` 并把 `_current_schema` ContextVar 设为租户 schema——之后**所有** SQL 通过 `fq_table` 按此 contextvar 加 schema 前缀。engine 内该鉴权调用有 91 处（每个公开引擎方法开头）。
- **MCP 面**：bank_id 解析链 = 路径段（单 bank 模式）> `X-Bank-Id` 头 > `HINDSIGHT_MCP_BANK_ID`（默认 "default"）；鉴权成功后 `_current_schema.set(tenant_context.schema_name)`（mcp.py:487-489），随后把 bank_id/租户/密钥/额外头全部放入 ContextVar（520-530），工具执行时经 resolver 组装 `RequestContext`。
- **Worker 面**：租户扩展的 `list_tenants()` 每轮轮询动态发现 schema（poller.py:404-408）；认领的行带着 `schema` 上下文，执行前 `task_dict["_schema"] = task.schema`（poller.py:1199-1200），engine 的 `_execute_task` 弹出并设 ContextVar（memory_engine.py:3969-3971）。worker/main.py:267-270 注释警示：租户扩展必须在建 engine 之前加载，否则 `_authenticate_tenant` 会把 schema 重置回 "public"，worker 写入落错 schema。
- **维护扇出**：迁移（`hindsight-admin run-db-migration` 遍历 base + 全部租户 schema）、备份/恢复、`delete_bank` 的扩展表清扫（`extra_bank_tables` 声明 `BankScopedTable` 描述符）都沿租户列表循环。
- bank 间无交叉查询；bank 级调度公平（3.2 的 bank 轮转）与 bank 级串行化（bank_serialization_sql）都在 SQL 谓词层实现，而非进程锁。

## 3.8 生产部署形态与多副本注意

- **单进程能跑**：`hindsight-api` + `HINDSIGHT_API_DATABASE_URL=pg0://...`（embedded Postgres，需 `embedded-db` extra）即全家桶——REST、MCP、poller、迁移（`run_migrations_on_startup` 默认开）都在一个进程。Docker standalone 镜像即此形态（外加同容器的控制面 node 进程）。
- **多副本要留意**：
  1. 迁移幂等，多个 uvicorn child 各自 import `server.py` 时都会跑一遍（server.py:69 注释明说 safe）；多副本 API 无共享内存状态（准入/信号量均为进程内 asyncio 对象），可横向扩。
  2. 准入限额按进程计（admission.py:33-35），容量规划按 `副本数 × 每进程限额`。
  3. `--workers N` 的 `/metrics` 需要 `HINDSIGHT_API_METRICS_WORKER_LABEL=true` 才能看到全部 worker 的序列（3.5）。
  4. worker 拆分：API 侧 `HINDSIGHT_API_WORKER_ENABLED=false` + 独立 `hindsight-worker`（helm 默认 worker.enabled=false，`values.yaml:136-138`，副本数 2）；worker_id 默认取 hostname，StatefulSet 用稳定 pod 名显式注入（worker-statefulset.yaml:62-66），容器里随机 hostname 会导致崩溃后"自己的行没人认领"（#3228，由 shutdown release + admin decommission 兜底）。
  5. 槽位是每 worker 进程的：`max_slots=10`、预留 `{consolidation: 2}` 是**单进程**值；扩 worker 数即可扩总并发。
  6. 超时族：`timeout_graceful_shutdown=5`（HTTP）、`shutdown_graceful(30s)`（poller drain）、retain/consolidation wall ceiling、`DB_ACQUIRE_WARN_THRESHOLD_MS=1000`——分层关停先 drain 任务、再归还行、再刷 tracing span（worker/main.py:392-424）。

---

# 第 4 层【必答问题速答】

1. **REST 端点地图 / MCP 工具**：见 1.4（95 条路由，8 大组）与 1.3（MCP 39 工具全量 / 单 bank 36）。
2. **retain/recall/reflect 同异步**：recall、reflect 只有同步形态（disconnect 可取消）；retain 是同步/异步二合一（`async` 参数），异步路径 HTTP 200 + body `async:true` + `operation_id`，客户端轮询 operations。幂等键仅 async retain 有（客户端自选 UUID 作 parent operation_id，重放不重复入库，memory_engine.py:21792-21819）；其他异步操作靠提交期 dedupe（bank 级或 payload 键级）。
3. **任务系统**：队列表 `async_operations`（3.1）；认领 = 轮询 schema 扫描 + rot/fifo 单语句 `FOR UPDATE SKIP LOCKED` + `mark_operations_processing`（3.2）；失败恢复 = retry/defer/backpressure/wall-timeout/startup-shutdown 双向回收（3.3）；worker 与 API 共表共代码，默认同进程、可分离。
4. **扩展槽位**：6 个（TENANT / OPERATION_VALIDATOR / HTTP / MCP / MEMORY_DEFENSE / MEMORIES），env-var 驱动的 import-string 加载协议 + config 前缀收集（3.6）。
5. **生产形态**：单进程可跑（pg0 内嵌）；多副本注意迁移幂等、准入按进程、metrics 多 worker 合并、worker_id 稳定性、按副本放大槽位（3.8）。
6. **admission 拒绝条件**：lane 启用且排队超 `max_wait_seconds`（recall 30s / reflect 5s / retain 2s 默认）→ 503 + Retry-After；排队中客户端断连 → 499；负数 env 关 lane；限额按 worker 进程计（3.4）。

# 第 5 层【未能确认 / 存疑】

1. （已结案）`async_operations.status` 的 CHECK 约束扩入 `cancelled`：初始迁移只列四态（5a366d414dce:237-239），`i4j5k6l7m8n9_add_cancelled_status_to_async_operations.py:28-31` 以 DROP 旧约束 + ADD 新约束的方式扩为五值（'pending','processing','completed','failed','cancelled'）。`cancelled` 的写入点：worker 侧父聚合置 cancelled 在 `worker/poller.py:984`，API 侧 `cancel_operation` 在 `engine/memory_engine.py:20907`（UPDATE 带 `status IN ('pending','processing')` 守卫重查）。
2. `OperationValidatorExtension.validate_consolidate / on_consolidate_complete`：接口已发布、api-slim 全库 grep 无调用点（子代理核实）；推测为闭源云版使用，但无法在 OSS 内证实其存在。
3. `mcp_tools.py` 顶部 docstring 称 `hindsight-local-mcp` 为 stdio transport，与 `mcp_local.py` 自身 docstring（HTTP transport，8888 端口）矛盾——按实现判断 docstring 过时，但不排除存在旧的 stdio 路径历史。
4. `hindsight-all-npm` 的 `server.ts` 流程（profile create → daemon start → /health 轮询）来自子代理阅读；`hindsight-embed` 各端口/命令解析顺序未逐一复跑。
5. `_claim_reserved_tasks`（非 consolidation 的预留池认领 SQL）未逐行核对，仅核实了共享池与 consolidation 两条认领路径及统一的 `claim_tasks` 两阶段结构。
