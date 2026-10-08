# 06 · API 层与服务形态

> 代码基线：`6d8b09678`（v0.10.3，2026-10-09）；增量窗口 `5b8356bb2..6d8b09678` 共 155 提交，涉及本篇的变更：worker 终态写库改限期重试+自杀（#5377/#5380）、append 冲突与 store 背压统一走 defer（#5396）、启动恢复改后台跑（#5411/#5407）、HTTP 指标按路由模板打标（#5063）、bank 缓存 TTL 内被删的 store 调用改 404（#5432）、DB 池慢获取阈值可配（#5397）、模板重导入跳过未变更项（#5404）、同步 retain 断连即取消（#5358/#4526）、Oracle `now() AT TIME ZONE` 重写（#5437）及全篇行号漂移。本篇已随 0.10.2 全量更新（原基线 `12f2d54f6`）。

> 研究对象：`hindsight-api-slim/hindsight_api/`（api/、worker/、main.py、server.py、daemon.py、mcp*.py、metrics*.py、tracing.py、liveness.py、loop_watchdog.py、cancellation.py）、`hindsight-extensions/`、发布包链（hindsight-api-slim → hindsight-api → hindsight-all* → npm 包）。
> 方法：全部论断均直接读码核实，标注 `相对路径:行号`（相对 `hindsight-api-slim/`，其他目录标注全路径）。无法确认的条目在文末单独列出。

---

# 第 1 层【全景】

## 1.1 一句话形态

Hindsight 的服务形态是一个**单体 FastAPI 进程**：`hindsight-api` 一个入口同时承载 REST HTTP 面、MCP 协议面（同进程 ASGI 中间件拦截 `/mcp*`——ASGI：Python 的异步 Web 服务接口标准，应用与中间件都是接收 scope 上下文和 receive/send 两条原始请求/响应字节流通道的可调用对象，中间件即包在应用外的另一层）、以及**默认开启**的后台任务执行器（进程内 `WorkerPoller`，`config.py:1873` `DEFAULT_WORKER_ENABLED = True  # API runs worker by default (standalone mode)`）。数据库（PostgreSQL/Oracle 23ai）既是存储也是任务队列；需要水平扩容时，把 `HINDSIGHT_API_WORKER_ENABLED=false` 关掉 API 内的 poller，另起独立的 `hindsight-worker` 进程，二者通过 `async_operations` 表解耦协作。

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
- **hindsight-api**（`hindsight-api/pyproject.toml:13,26`）`dependencies = ["hindsight-api-slim[all]==0.10.3"]`，且 `packages = []` —— 纯依赖伞，唯一作用是给"想要开箱即用（含本地 ML 模型 + pg0）"的用户一个名字；四个 scripts 在它这里重新声明一遍（19-23 行），保持 CLI 命令与包名一致。
- **hindsight-all / hindsight-all-slim**（`hindsight-all/pyproject.toml:12-16`、`hindsight-all-slim/pyproject.toml:12-16`）**不依赖 hindsight-api**，直接拉 `hindsight-api-slim`（all 版带 `[all]` extra，slim 版不带 extras）+ `hindsight-client` + `hindsight-embed`；它们提供的是 Python 库形态的嵌入式用法（`hindsight-all/hindsight/embedded.py` 的 `HindsightEmbedded`，自动拉起本地 daemon）。
- **@vectorize-io/hindsight-all**（`hindsight-all-npm/package.json:2-3`，无 `bin`）是程序化生命周期管理器：`src/command.ts:19-25` 返回 `["uvx", "hindsight-embed@<version>"]`（uvx：uv 生态的 npx——临时运行 PyPI 包的命令），`src/server.ts` 依次执行 profile 创建 → `daemon start` → 轮询 `http://127.0.0.1:8888/health`（30s 预算）。
- **hindsight-embed**（`hindsight-embed/pyproject.toml:18`，入口 `hindsight_embed.cli:main`）负责本地 daemon/profile：数据库默认 `pg0://hindsight-embed-{profile}`，API 默认 8888，控制面端口 = API 端口 + 10000；找 API 命令的顺序是：dev 仓库 `uv run` → 已安装的 `hindsight-api` 可执行文件 → 兜底 `uvx hindsight-api@<version>`（`hindsight_embed/daemon_embed_manager.py:477-517`）。

> 命名注意：`hindsight-api-slim` 的 "slim" 指**不带重量级可选依赖**（torch/本地模型等在 extras 里），而不是功能精简——核心引擎代码 100% 在这个包里。

## 1.3 对外协议面：REST + MCP，同一个进程、同一个 engine

先明确两个贯穿全文的名词：**bank**（记忆库）是 Hindsight 的隔离存储单元，相当于一个 agent 独立的"大脑"，URL 中的 `{bank_id}` 即其标识；**租户（tenant）**是 bank 之上的部署级隔离，映射到独立的数据库 schema。

- **REST**：全部业务路由挂在 `/v1/default/banks/{bank_id}/...` 前缀下（`default` 是租户占位段——`TenantExtension` 决定它映射到哪个 schema），仅两条例外：bank 前缀之外的 `GET /v1/default/chunks/{chunk_id:path}`（8037）与 `GET /v1/default/files/download/{key:path}`（9347）；另有 `/health`、`/health/ready`、`/health/live`、`/metrics`、`/version`、`/v1/bank-template-schema`。扩展 HTTP 路由挂 `/ext/`（http.py:5303-5318）。
- **MCP**：FastMCP 实现的 streamable HTTP/SSE 服务，挂在**同一个 FastAPI app 之外包一层 ASGI 中间件**（`api/__init__.py:97-105`），路径 `/mcp`（多 bank 模式，39 个工具）与 `/mcp/{bank_id}`（单 bank 模式，36 个工具，见 `mcp.py:131-166`）。工具清单在 `mcp_tools.py:43-84` 的 `_ALL_TOOLS`（39 个）：`retain`/`sync_retain`/`recall`/`reflect`、mental-model 全套 CRUD+`refresh`/`clear`、directives、memories/documents/operations/tags 的 list-get-update-delete、bank 管理、knowledge-base 树与页 CRUD。工具 schema 有一处刻意的"去类型化"：mental-model 触发器的 `tag_groups` 参数在 schema 里是普通 JSON 对象数组（`list[dict]`，`mcp_tools.py:159`）而非嵌套 Pydantic 模型——TagGroup 自引用（and/or/not 递归持有子组）会让 JSON Schema 递归、部分 LLM provider 因此拒收整个工具清单（#5013/#5244），形状校验改由 `field_validator` 把 dict 回穿 TagGroup 适配器完成（`mcp_tools.py:235-244`）。0.10.3 起 recall 工具的返回改紧凑 JSON（去掉 `indent=2`——缩进约占 recall 响应体积的五分之一，而该响应整体落进调用方 agent 的上下文，`mcp_tools.py:1347-1350`），工具 docstring 同时澄清 `max_tokens` 只计每条结果的 `text`、id/tags/scores/dates 不计。MCP 与 REST 共享同一个 `MemoryEngine` 实例，不是独立服务。
- **根级 `mcp_local.py`**：不是独立 MCP server，而是给本机 Claude Code 用的"一条命令起全家"薄壳——默认 `pg0://hindsight-mcp` 数据库，然后直接调用 `hindsight_api.main.main()`（`mcp_local.py:29-40`），完整 API 跑在 8888，MCP 端点是 `http://localhost:8888/mcp/`（HTTP transport，见其 docstring 1-24 行；`mcp_tools.py` 顶部 docstring 仍称它为 stdio transport，与实际实现不符，属文档残留）。

## 1.4 REST 端点地图（按资源分组）

以下为 `_register_routes`（http.py:5423 起）注册的全部业务路由（省略共同前缀 `/v1/default`；方法后为 handler 名，数字为定义处行号）。这张地图有两个用途：作为后续各小节的公共底图（retain/recall/operations 均会回指），以及开发时按 handler 名/行号直接跳转源码。顺读只需关注四组——**监控/元信息、Banks 与配置、Memory 写入与查询、异步操作管理**——它们覆盖主流程；其余分组按需查阅。v0.10.2 相比上一版新增 4 条路由（bank aliases 全套），总数 99 条；v0.10.3 路由数不变（仍 99 条），但 recall 的超长查询不再以 HTTP 400 拒绝——改为引擎入口统一按 token 上限截断（#5388：客户端按字符数裁剪查询，coding-agents 插件 2000 字符的提示词曾因此被直接拒之门外）。

**监控/元信息**
- `GET /health`、`GET /health/ready`（就绪，查数据库，5638）、`GET /health/live`（存活，不碰数据库，5655）、`GET /metrics`（Prometheus，5714，渲染已移出事件循环，见 3.5）、`GET /version`、`GET /v1/bank-template-schema`

**Banks 与配置**
- `GET /banks`（列表，6553）、`PUT /banks/{bank_id}`（创建或更新，8554，`@audited("create_bank")`）、`PATCH /banks/{bank_id}`（8599）、`DELETE /banks/{bank_id}`（8645）
- `GET /banks/{bank_id}/aliases`（8437）、`POST /banks/{bank_id}/aliases`（8460，201）、`PATCH /banks/{bank_id}/aliases/{alias}`（8491，设展示别名）、`DELETE /banks/{bank_id}/aliases/{alias}`（8526）——bank 别名全套，v0.10.2 新增（#4706/#4724，见 3.9）
- `GET /banks/{bank_id}/stats`（6581）、`GET /banks/{bank_id}/stats/memories-timeseries`（6674）、`GET/PATCH/DELETE /banks/{bank_id}/config`（层级配置，写受 `HINDSIGHT_API_ENABLE_BANK_CONFIG_API` 控制，DELETE 重置 9636）、`POST /banks/{bank_id}/clone`（9210，整库克隆，202 异步）、`GET /banks/{bank_id}/tags`（7970）、`POST /banks/{bank_id}/health/llm`（6639，刻意做成 POST——会真实调用一次 provider）
- legacy 的 `GET/PUT /banks/{bank_id}/profile`（8372/8385）与 `POST /banks/{bank_id}/background`（8400）已退役（保留路由恒返 410）

**Memory 写入与查询**
- `POST /banks/{bank_id}/memories`（retain，同步/异步二合一，10010）、`POST .../memories/dry-run-extract`（5888）、`GET .../memories/list`（5781，列表+全文搜索）、`POST .../files/retain`（10315，文件转换后 retain）、`GET /v1/default/files/download/{key:path}`（9347，附件/导出 ZIP 下载）
- `POST .../memories/recall`（检索，6131，handler `api_recall` 6145）、`DELETE /banks/{bank_id}/memories`（清空 bank 全部记忆，10484）
- `GET .../memories/{memory_id}`（6012）、`GET .../memories/{memory_id}/history`（6100）、`PATCH .../memories/{memory_id}`（6044，update_memory；**invalidate 也是 PATCH**，body 带 `state: "invalidated"`，请求模型 2698-2712——没有独立 DELETE 单条路由）、`DELETE .../memories/{memory_id}/observations`（9539，清空该记忆的观察）
- `POST /banks/{bank_id}/reflect`（处置感知推理，6374，handler `api_reflect` 6389）

**文档/实体/观察**
- `GET /banks/{bank_id}/documents`（7762）、`GET/PATCH/DELETE .../documents/{document_id:path}`（7933/8076/8128）、`GET .../documents/{document_id:path}/chunks`（7834）、`POST .../documents/{document_id:path}/reprocess`（7890）、`GET /banks/{bank_id}/attachments/{attachment_id}`（9293，附件元数据读取）
- `GET /banks/{bank_id}/graph`（5740，bank 级图）、`GET /v1/default/chunks/{chunk_id:path}`（8037，bank 外的 chunk 寻址读）
- `GET /banks/{bank_id}/entities`（6709，支持 tag 过滤）、`GET .../entities/graph`（6757）、`GET .../entities/{entity_id}`（6799）、`POST .../entities/{entity_id}/regenerate`（6856）——实体为只读派生数据，无更新/删除端点
- `DELETE /banks/{bank_id}/observations`（清空观察，9420）、`GET /banks/{bank_id}/observations/scopes`（9445）

**Mental Models / Directives / Knowledge Base**
- `GET/POST /banks/{bank_id}/mental-models`（6881/7013）、`GET/PATCH/DELETE .../mental-models/{id}`（6944/7180/7223）、`POST .../{id}/refresh`（7060，异步任务）、`POST .../{id}/clear`（7144）、`POST .../{id}/dry-run-refresh`（7094）、`GET .../{id}/history`（6982）
- `GET/POST /banks/{bank_id}/directives`（7581/7660）、`GET/PATCH/DELETE .../directives/{directive_id}`（7630/7695/7732）
- `GET .../knowledge-base/tree|search|export`（7263/7418/7375，树与搜索支持 tag 过滤）、`POST .../knowledge-base/folders`（7290，创建文件夹）、`POST .../knowledge-base/pages`（7322，创建页面）、`GET .../knowledge-base/pages/{page_id}`（7453，空页返回空正文而非占位句，#4680）、`PATCH/DELETE .../knowledge-base/nodes/{node_id}`（7481/7550；全空 body 的 PATCH 被 engine 以 400 拒绝在任何 bank 读取之前，#4709）

**异步操作管理（任务系统的 API 面）**
- `GET /banks/{bank_id}/operations`（8171）、`GET .../operations/{operation_id}`（8219）、`DELETE .../operations/{operation_id}`（取消 pending/processing，8257）、`POST .../operations/{operation_id}/retry`（重排队 failed，8294）、`DELETE .../operations/{operation_id}/delete`（删除终态记录，8324）

**Consolidation**
- `POST /banks/{bank_id}/consolidate`（9663，手动触发整合）、`POST .../consolidation-strategies/preview`（9479，试跑整合策略；策略体已类型化为 `ConsolidationStrategySpec` 列表，#4654）、`POST .../consolidation/recover`（9514）

**Bank Transfer（整库搬运，v0.10.2 头部特性，见 3.9）**
- `POST .../transfer/export`（9020，202 异步导出，三个 include 标志 + 可选 document_id 子集）、`POST .../transfer/import`（9104，202 异步导入，`mode=restore|merge`）
- `POST .../clone`（9210，202 异步克隆 = 导出+导入在本进程背靠背）
- 文档级历史端点：`POST .../document-transfer`（8946，异步文档导入提交）、`POST .../document-transfer/export`（8882，异步文档导出提交）、`GET .../document-transfer`（8852，同步全量导出已退役，恒返 410）
- 轮询走 `GET .../operations/{operation_id}`（8219），ZIP 经 `GET /v1/default/files/download/{key:path}`（9347）下载——transfer 面没有 GET 形态的提交端点

**Bank 模板（配置面，非归档）**
- `GET /banks/{bank_id}/export`（8764，导出模板 manifest：config + mental models + directives）、`POST .../import`（8677，导入模板 manifest；0.10.3 起重导入跳过未变更的 mental models/directives，见 3.9）——注意这两个是**模板**交换，与 Bank Transfer 的整库归档无关

**Webhooks / 审计 / LLM 观测 / 提示词**
- `GET/POST /banks/{bank_id}/webhooks`（9787/9726）、`PATCH/DELETE .../webhooks/{id}`（9881/9850，无单条 GET；读取靠列表与 deliveries）、`GET .../webhooks/{id}/deliveries`（9964）
- `GET /banks/{bank_id}/audit-logs`（10530，+ `GET .../audit-logs/stats` 10571）、`GET /banks/{bank_id}/llm-requests`（10604，+ `GET .../llm-requests/stats` 10662）、`POST /banks/{bank_id}/prompts/preview`（5947）

MCP 端能做的操作与上述 REST 面**大体对应**（共享 engine），差异只在工具粒度：MCP 没有 export/import/transfer/clone/consolidate 这类管理型操作；反向地，MCP 的 `list_banks`/`create_bank`/`get_bank`/`get_bank_stats` 直连 engine，没有一一对应的 REST 形态——**没有 `GET /banks/{bank_id}` 单体读取端点**；bank 详情从 `GET /banks` 列表与 `/stats`、`/config` 获取，PATCH/DELETE 只承担写。

---

# 第 2 层【主流程】

## 2.1 服务启动：从 `hindsight-api` 到 uvicorn.run

`hindsight_api/main.py:277` 的 `main()` 按以下顺序装配（`hindsight_api/server.py` 则是给 `uvicorn hindsight_api.server:app` 用途准备的模块级等价物）：

1. `load_dotenv_for_entrypoint()` 加载 .env → `_get_raw_config()` 读配置；
2. 解析 CLI 参数（`_parse_cli_args`，`--host/--port/--workers/--daemon/...`）；
3. **端口预探测**：`_wait_for_port`（main.py:325 调用，定义 172-180，对 EADDRINUSE 做 5s 宽限重试）在昂贵的初始化（pg0 启动、模型加载、迁移）**之前**试绑定端口，避免"初始化 10 秒后才发现端口被占"造成的重启风暴（main.py:142-152 注释引用 #4281）；
4. daemon 模式：`daemonize()`（daemon.py:94-147）用 `subprocess.Popen` + `_HINDSIGHT_DAEMON_CHILD` 环境变量**重 exec 自己**进入后台（不用 double-fork，注释写明原因是 macOS 上 fork 不 exec 会破坏 Apple 框架状态导致 PyTorch/MPS SIGBUS）；
5. 加载两个由环境变量指定的扩展：`OPERATION_VALIDATOR` 与 `TENANT`（main.py:367-375）；
6. 构建 `MemoryEngine` + `create_app`（仅单 worker 模式，main.py:398-416），多 worker/reload 模式改为传 import string `"hindsight_api.server:app"`，由每个 uvicorn 子进程自行导入（uvicorn_config 组装处 main.py:453）；
7. `uvicorn.run(...)`（main.py:492）。

main.py 顶部有一段关键的懒加载设计（main.py:44-58）：`create_app`/`MemoryEngine`/扩展机制**不在模块级导入**，通过模块 `__getattr__`（PEP 562）在首次使用时解析：

```python
# hindsight_api/main.py:60-66
_LAZY_IMPORTS: "dict[str, tuple[str, str]]" = {
    "MemoryEngine": (".", "MemoryEngine"),
    "create_app": (".api", "create_app"),
    "OperationValidatorExtension": (".extensions", "OperationValidatorExtension"),
    "TenantExtension": (".extensions", "TenantExtension"),
    "load_extension": (".extensions", "load_extension"),
}
```

逐点讲解：
- 原因是 uvicorn 多进程 supervisor 用 **spawn**：每个子进程会重跑 `sys.argv[0]`（pip console-script 包装器），其首行就是 `from hindsight_api.main import main`——这个模块 import 什么，**每个子进程就都要付一次代价**。注释里给了实测：`.api` 导入约 6.2s、`.extensions` 约 2.6s，把这两者移出模块级后整条命令的导入从 6578ms 降到 312ms（main.py:53-55）。
- 子进程导入若超过 supervisor 的 5s 健康检查会被 SIGKILL 无限重生（这正是被修掉的 respawn bug）。
- 走 `main()` 时通过 `sys.modules[__name__]` 属性访问触发懒导入（main.py:359-364），而不是裸 `from .x import y`——后者会屏蔽测试对模块属性的 patch。

uvicorn 配置里两个值得注意的点（main.py:452-455）：`ws="wsproto"`（避开 websockets 的弃用告警）、`timeout_graceful_shutdown=5`（优雅关闭上限 5s，二次 Ctrl+C 强杀）；`--workers N` 时把 N 写回 `os.environ[ENV_WORKERS]`（main.py:463-466），让每个子进程按 CPU 预算分摊准入限额（见 3.4）。

`server.py`（94 行）是 import-string 模式的目标模块：模块级 `load_dotenv_for_entrypoint()`（server.py:25-27）放在 **import MemoryEngine 之前**——引擎子模块（如 llm_wrapper）在 import 时就会按配置创建信号量，而配置来自环境变量，所以必须先加载 .env，配置才能读到；随后是 profiling 装配（29-40）、扩展加载（58-65）、以及**模块级**构建 `MemoryEngine(run_migrations=config.run_migrations_on_startup)` 与 `app = create_app(...)`（70-87）——注释说明迁移是幂等的，多 worker 各自导入也安全。

### 启动时序（含 FastAPI lifespan）

```mermaid
flowchart TD
    A["hindsight-api CLI<br/>main.py:277"] --> B["load_dotenv + 解析 CLI 参数"]
    B --> C["端口预探测 _wait_for_port<br/>main.py:325"]
    C -->|"--daemon"| D["daemonize 重 exec 后台化<br/>daemon.py:94"]
    D --> E
    C -->|"前台"| E["加载 OPERATION_VALIDATOR / TENANT 扩展<br/>main.py:367-375"]
    E --> F{"--workers 大于 1 ?"}
    F -->|"否"| G["本进程建 MemoryEngine + create_app"]
    F -->|"是"| H["传 import string<br/>每个子进程自建"]
    G --> I["uvicorn.run"]
    H --> I
    I --> J["lifespan 启动<br/>http.py:5081"]
    J --> K["loop_lag 探针 + OpenTelemetry（OTel）指标<br/>http.py:5097-5121"]
    K --> L["initialize_tracing<br/>http.py:5125"]
    L --> M["memory.initialize：建池 + 加载模型 + 跑迁移<br/>（部分维护例程随迁移安装，见 2.5 步骤 1）<br/>http.py:5130"]
    M --> N["loop_watchdog + 进程内 WorkerPoller<br/>http.py:5142-5176"]
    N --> O["tenant/http 扩展 on_startup<br/>http.py:5179-5184"]
    O --> P["开始服务流量"]
```

其中 N 节点（`loop_watchdog + 进程内 WorkerPoller`，http.py:5142-5176）在 0.10.3 有一处行为变化：进程内 poller 与独立 worker 一样，启动恢复不再阻塞轮询（见 3.3 的 #5411）。

## 2.2 create_app：一个 app、三层装配

真正的 app 由两段代码拼成：

**第一段（`api/__init__.py:17-109`，统一入口）**：`create_app(memory, http_api_enabled, mcp_api_enabled, mcp_mount_path, initialize_memory)`——先建两个 FastMCP server（多 bank / 单 bank），再调 `api/http.py` 的 `create_http_app` 建 REST app，然后：① 用 `chained_lifespan`（`api/__init__.py:79-92`）把两个 MCP server 的 lifespan 包在 REST app 的 lifespan 外层——启动时先起 MCP、再起 REST，关闭顺序相反（lifespan 即 FastAPI/ASGI 的应用启动/关闭生命周期钩子）；② `app.add_middleware(MCPMiddleware, ...)` 把 MCP 中间件包在最外层（97-105 行）。注释解释不用 Starlette Mount 的原因：Mount 会让无尾斜杠的 `/mcp` 吃 307 重定向。

**第二段（`api/http.py:5043-5330`，本文件也叫 `create_app`，`api/__init__.py` 里以 `create_http_app` 别名导入）**：`create_app` 依次做入口处 profiling 装配（5069-5073，保证 `--workers N` 时每个子进程也装上 cProfile）、route_class 替换（`ExcludeNoneRoute`→`UnknownParamsRoute`，5253/5293）、GZip（5263-5265，阈值可配，负值整体关闭）、OpenAPI ValidationError 补丁（5274-5285）、**准入控制器**挂 `app.state.admission`（5297）、注册全部路由（5300）、`/ext/` 扩展路由挂载（5303-5318，include_router 前先 `use_unknown_params_routes` 让扩展路由也走 UnknownParamsRoute）、两个纯 ASGI 中间件（见下）、OTel ASGI instrumentation（5329）。

```python
# hindsight_api/api/http.py:5317-5327
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
- Starlette 的 `add_middleware` 是插到栈顶（最后添加者在最外层），所以**自外向内**的完整顺序是：`MCPMiddleware` → `HttpObservabilityMiddleware` → `ClientDisconnectCancellationMiddleware` → `GZipMiddleware` → route_class（`UnknownParamsRoute`）→ FastAPI 依赖解析 → handler。上述是 tracing 关闭（默认）时的全部；**tracing 开启时** OTel ASGI 中间件经 `_instrument_app_for_tracing`（http.py:5378-5418）注入，位于 HttpObservability 之外、MCPMiddleware 之内——即 MCP → OTel → Observability → Disconnect → GZip。
- 两个 ASGI 中间件都是纯 ASGI 实现，刻意替换掉了原来的两个 `@app.middleware("http")`（BaseHTTPMiddleware）：注释给出量级——BaseHTTPMiddleware 的每请求子任务 + memory-stream 中转在便宜路由上吃掉约 3 倍吞吐（5285-5290）；`api/observability.py:5-9` 记录 `/health/live` 从约 1500 rps 升到 5100 rps、p99 159ms→33ms。
- `ClientDisconnectCancellationMiddleware` 的位置约束是"必须能读到原始请求字节流（receive 通道）"：任何包装 receive 的层（BaseHTTPMiddleware、OTel 的 receive span）都在它之外才能感知断连。它外面的 `HttpObservabilityMiddleware` 只包 send 不包 receive，OTel 又显式 `exclude_spans=["receive","send"]`（http.py:5416，省 span 的同时避免包装 receive 破坏断连检测），所以最外层是 MCP → observability、断连中间件在第三层也依然有效。

## 2.3 请求 → 中间件 → 依赖 → engine：分层机制图

```mermaid
flowchart TD
    C["Client SDK / curl / MCP 客户端"] -->|"HTTP :8888"| MW1["MCPMiddleware<br/>前缀 /mcp 拦截，否则透传<br/>mcp.py:421"]
    MW1 -->|"REST 路径"| MW2["HttpObservabilityMiddleware<br/>记 hs_asgi_t0 + http 指标（按路由模板打标）<br/>observability.py:133"]
    MW2 --> MW3["ClientDisconnectCancellationMiddleware<br/>recall/reflect/retain POST 装断连令牌<br/>disconnect.py:66"]
    MW3 --> MW4["GZipMiddleware<br/>阈值 gzip_min_size"]
    MW4 --> ROUTE["route_class UnknownParamsRoute<br/>未知参数收集 + X-Ignored-Params"]
    ROUTE --> DEP["FastAPI 依赖解析<br/>get_request_context → precheck_for → admit_for<br/>http.py:5430/5526/5487"]
    DEP -->|"拒绝: 503/402/401，body 未读"| OUT["返回错误"]
    DEP -->|"放行"| HANDLER["handler: api_retain / api_recall / api_reflect<br/>加 @audited 装饰器"]
    HANDLER --> ENG["MemoryEngine<br/>_authenticate_tenant → _validate_operation → 业务"]
    ENG --> MEMO["MemoriesExtension（存储层插槽，见 3.6）<br/>默认 PostgresMemories"]
    MEMO --> DB[("PostgreSQL / Oracle 23ai")]
    MW1 -->|"/mcp 路径"| FASTMCP["FastMCP multi/single-bank server<br/>contextvars 传 bank_id/租户"]
    MW1 -.-> OTELNOTE["注：tracing 开启时 OTel ASGI 中间件<br/>插在 MCPMiddleware 与 Observability 之间<br/>http.py:5378-5418"]
    FASTMCP --> TOOLS["mcp_tools.py 39 个工具<br/>直接调 engine 方法"]
    TOOLS --> ENG
```

依赖链的三个环节是理解每条重路由的关键（都在 `_register_routes` 内定义）：

1. **`get_request_context`**（http.py:5430-5467）：从 `Authorization` 头取 API key（支持 `Bearer <key>` 与裸 key），并把 `HINDSIGHT_API_EXTENSION_PASSTHROUGH_HEADERS` 白名单中的头收进 `RequestContext.extra_headers`（默认空名单）。第一行 `request.scope.setdefault("hs_deps_t0", time.time())` 是给 recall 的分段计时埋点。
2. **`precheck_for(operation)`**（http.py:5526-5601）：FastAPI **先解析依赖、后反序列化 body**（5533-5535 注释），所以它先 `memory._authenticate_tenant(request_context)`（5564，租户鉴权 + schema 定位），再调 `OperationValidator.precheck`（5582）——扩展可以在这里以 402/429 等拒绝请求，而请求体从未被读进内存。挂在 8 条计费路由上：dry-run-extract、recall、reflect、mental-model 创建/刷新/dry-run-refresh、retain、files/retain（http.py:5907/6150/6394/7028/7073/7119/10041/10352）。
3. **`admit_for(operation)`**（http.py:5487-5524）：yield 风格依赖，`async with controller.admit(...)` 让准入许可覆盖整个请求生命周期；排队期间如果客户端断连（`AdmissionAbandoned`）直接 499 关闭，超时（`AdmissionRejected`）回 503 + `Retry-After`。

## 2.4 贯穿例子 A：一次 POST retain 的完整 HTTP 旅程

请求：`POST /v1/default/banks/my-bank/memories`，body `{"items":[{"content":"..."}],"async":true,"operation_id":"<client uuid>"}`。

1. **中间件段**：MCPMiddleware 前缀不匹配直接透传；HttpObservabilityMiddleware 记 `scope["hs_asgi_t0"]` 并把请求按**路由模板**计入 `hindsight.http.duration`（见 3.5）；断连中间件的监控面是 `/memories/recall`、`/reflect` 结尾的路径加上 **`POST /memories`**（0.10.3 起 #5358：同步 retain 没有操作行也没有应答，一旦开始就没有别的东西能停它——被放弃的同步 retain 曾实测跑满 1880s、全程占着一个 LLM 槽，还往一个中途被删的 bank 里提交，issue #4526；recall/reflect 拿协作令牌，sync retain 则由 handler 用 `run_task_cancellable_on_disconnect` 直接取消整个任务）；GZip 视大小压缩请求响应。
2. **依赖段**：`get_request_context` 提取 API key → `precheck_for(RETAIN)`：租户鉴权（`_authenticate_tenant`，把 `_current_schema` ContextVar——请求作用域的上下文变量，可理解为协程版的 thread-local——设为租户 schema）+ validator `precheck`（可 402 拒费）→ `admit_for(RETAIN)` 拿准入许可（默认 32/核并发、排队上限 2s）。
3. **body 解析**：`RetainRequest` 校验（items/document_tags/async_/operation_id...）。
4. **handler `api_retain`**（http.py:10036-10313，`@audited("retain")` 在 10035）：
   - 多模态内容先规范化：`canonicalize_item_content` 把图片块落成内容寻址附件、正文替换占位符（10079-10088），并处理"文档重发旧占位符"的编辑场景（10054-10062）；附件按文档归属入库（10089-10104），validator 拒绝时的附件回收在 engine 侧（memory_engine.py:22319）。
   - items 按 `strategy` 分组，逐条拼 `content_dict`（timestamp→event_date、context、metadata、document_id、entities、tags、observation_scopes、update_mode...，10107-10161）。
   - **async=true 分支**（10159-10190）：每个策略组调一次 `memory.submit_async_retain(...)`（10171），返回 `RetainResponse{success, bank_id, items_count, async: true, operation_id(s)}`。注意：**HTTP 状态是 200**（路由未设 202），异步语义靠 body 里的 `async: true` + `operation_id` 表达，客户端轮询 operations 端点。
   - **async=false 分支**（10191-10242）：若 batch API 开启则强制拒绝同步模式（400，10195-10203）；否则逐组 `retain_batch_async` 同步执行（协程化的 `_retain_groups` 定义在 10212，调用 10215），聚合 token usage 后返回。0.10.3 起整个同步执行被包进 `run_task_cancellable_on_disconnect`（10243-10249，http.py:404-451 定义）：与 recall/reflect 的"阶段边界轮询令牌"不同，同步 retain 的墙钟几乎全花在**单个 await 上**（先排队 LLM 并发信号量、再等 provider 响应），检查点永远轮不到，只有真正的任务取消能解锁它；取消路径是管线本就支持的形态（worker 的绝对 wall 上限、orchestrator 的 finally 提交已产出子批并停掉在飞抽取任务——见 02 篇 3.11 的 #5372/#5387）。异步路径不受影响——它在任何抽取开始前就返回，`DELETE /operations/{id}` 负责停它。
5. **错误映射**（10241-10313）：validator 拒绝→其自带 status_code；operation_id 冲突→409；视觉不支持→422；参数错误→400；`MemoryDefenseAllBlockedError`→422 + violations 明细；其余→500（附输入摘要与 traceback）。

`submit_async_retain`（memory_engine.py:22274 起）内部：租户鉴权 → validator `validate_retain` → `sanitize_value` 清洗（防 U+0000/孤立代理对让 jsonb INSERT 直接炸，22327-22341）→ **幂等快路径**（22343-22360）→ 拒绝重复 document_id（仅异步路径，22361-22380）→ 按 token 预算 `_split_contents_into_async_children` 切子批（22391）→ 总是建 parent operation（22409-22411，"even for single batch - simpler, more reliable code path"）→ parent+子操作同一事务逐个入库 → 通知 task backend。

幂等键机制的完整规则：

```python
# hindsight_api/engine/memory_engine.py:22246-22273（节选）
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
- 客户端可自选 UUID 作 `operation_id`；重发同一 id 时若该 id 已是**本 bank 的 batch_retain parent**，直接回放原响应（`items_count` 存在 parent 的 `result_metadata` 里），**不产生新工作**——这是"丢失应答后重试不重复入库"的保证（22291-22296 docstring：parent 主键本身就是并发权威，无需额外去重列）。
- id 被其他 bank 或其他操作类型占用 → `RetainOperationConflictError` → HTTP 409。
- 该 SELECT 刻意不在创建事务里（22346-22357 注释）：并发首次提交靠主键冲突兜底，快路径只为常见的顺序重试省事。
- 其他异步操作（consolidate、refresh_mental_model、export/import 等）没有客户端幂等键，靠的是**提交期去重**（`dedupe_by_bank` / `dedupe_in_flight_payload_key`，见 3.1）。

## 2.5 贯穿例子 B：worker 认领一次 retain 任务的数据库操作全程

前置：API 已把任务写入队列。worker 侧（`worker/main.py:169` 的 `main()`，或 API 进程 lifespan 里的同款 `WorkerPoller`）以 `poll_interval_ms`（默认 500ms，config.py:1875）循环执行。

**步骤 1 — 找有活的 schema**（多租户时）：`_scan_active_schemas`（poller.py:448-484）优先调用服务端例程 `SELECT * FROM public.schemas_with_pending_work()`（481 行，一次往返替代 N 次逐 schema EXISTS）。注意这个例程**不由 Hindsight 安装**——`engine/db/optional_routines.py` docstring 明言 API 与迁移都不装、由运维带外安装（如 Helm hook）；未安装时回退 `_scan_active_schemas_by_exists`（486 起）的 per-schema `EXISTS`（单 schema 部署即如此）。

**步骤 2 — 计算本进程可用槽位**：`_get_available_slots`（poller.py:504-534）。`max_slots` 默认 10（config.py:1879），`slot_reservations` 默认 `{"consolidation": 2}`（config.py:957）—— 预留池保底、剩余进共享池。

**步骤 3 — 声明式认领**（`claim_batch` → `_claim_batch_for_schema_inner`，poller.py:562/708-768）：整轮复用一条池化连接（claim_batch 内 589-599 注释：每次 acquire/release 都有 session GUC + RESET ALL 仪式，逐 schema 取连接会把 2 条有效查询放大成约 12 条语句，#3499），但每个 schema 的认领**各自开事务**（727 行），行锁只到本 schema 认领提交为止。认领 SQL 由 `backend.ops.claim_tasks` 生成（ops_postgresql.py:1879-1952），核心是 `FOR UPDATE SKIP LOCKED`（已被其他 worker 锁住的行直接跳过、不等待，所以多 worker 并发认领互不阻塞）：先按预留池逐类型认领（consolidation 走优先级分层的 `_claim_consolidation_tasks`），再认领共享池。

示例（字段名与代码一致）：`async_operations` 中一行。`serialization_key` 是认领层的串行化分组键：同组作业不并发（异步 retain 按文档 id 填入，consolidation 按 bank 填入），折叠也按它找兄弟。
```
operation_id=7f3c...  bank_id='my-bank'  operation_type='retain'
status='pending'  task_payload={"type":"batch_retain","operation_id":"7f3c...","bank_id":"my-bank",
              "contents":[{"content":"..."}],"_traceparent":"00-4bf9..."}
serialization_key='doc-42'  retry_count=0  next_retry_at=NULL
```
共享池认领 SQL（ops_postgresql.py:1848-1877，rot/fifo 两 CTE 一条语句）：

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

随后（仍在 claim 事务内）：`_fold_retain_peers`（poller.py:768-865）把同一 `serialization_key`（同一文档）排队中的兄弟 retain **折叠**进本次执行——`fetch_foldable_retain_peers`（ops_postgresql.py:1973 起，同样 `FOR UPDATE SKIP LOCKED`）取 peers，`plan_retain_fold` 按 token 预算规划，失败则退回单任务执行。落账分两次：主行在 `claim_tasks` 末尾统一 `mark_operations_processing`（ops_postgresql.py:1950 调用，定义 1954-1972），折叠并入的 peers 由 `_fold_retain_peers` 再补一次 mark（poller.py:845-850）。落账语句：

```sql
UPDATE async_operations
SET status = 'processing', worker_id = $1, claimed_at = now(), updated_at = now()
WHERE operation_id = ANY($2)
```

**步骤 4 — 执行**：poller 把行包成 `ClaimedTask`（payload 反 JSON、注入 `_retry_count/_operation_id`、DB 权威的 `operation_type`，poller.py:760-771），`asyncio.create_task` 火后不管（1141-1158），经 `_run_executor` 的 wall-clock 上限包装（retain 绝对上限、consolidation 是"无进展才计时"的空闲上限，poller.py:76-112, 1173-1230）调 `MemoryEngine.execute_task`。engine 侧：恢复 payload 里的 traceparent 接回原 trace（memory_engine.py:4614-4633）、设 `_schema` ContextVar（4640-4642）、**先查行状态是否已被取消**（4644-4658），然后按 `type` 分发到 `_handle_batch_retain` 等处理器（4670-4695）。

**步骤 5 — 终态**：成功 → `_mark_all_completed` 经 `_write_terminal` 落盘（fold 的全部 operation_id 一起终态，v0.10.2 起同时置 `error_message=NULL` 清掉陈旧错误，#4880；0.10.3 起该写本身也带限期重试，见 3.3）；失败 → `_mark_failed`（poller.py:913-939，`WHERE status <> 'cancelled'` 防把取消改成失败）；二者都在事务内做 batch_retain 父子聚合 `_maybe_update_parent_operation`（941-1044：FOR UPDATE 锁父行，全部子件到达终态时父行置 completed/failed/cancelled，failed 优先且继承最高频子错误）。

**步骤 6 — 客户端收尾**：`GET /banks/{bank_id}/operations/{operation_id}` 轮询（8171/8219）；失败后可 `POST .../retry` 重排队（8294），pending/processing 可 `DELETE .../{operation_id}` 协作式取消（8257：pending 永不启动，processing 由执行中任务在下个检查点停下）。

### 任务状态机

```mermaid
stateDiagram-v2
    [*] --> pending: INSERT，payload 原子携带
    pending --> processing: 认领 + mark_operations_processing
    processing --> completed: mark_completed
    processing --> failed: mark_failed，guard 排除 cancelled
    processing --> pending: schedule_retry，retry_count 加一
    processing --> pending: defer（store 背压/append 冲突），不计 retry
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

**operation_type 清单**（代码中出现者）：`retain`（= batch_retain 子件，payload 的 `type` 字段为 `"batch_retain"`，`memory_engine.py:22038-22041`）、`batch_retain`（父聚合行，payload 为 NULL 不可认领）、`file_convert_retain`、`consolidation`、`refresh_mental_model`、`webhook_delivery`、`import_documents`/`export_documents`/`import_bank`/`export_bank`、`clone_bank`（memory_engine.py:7879）、`graph_maintenance`、`vector_index_maintenance`。

**三种 task backend**（`engine/task_backend.py`）——注意它们的分工是"提交侧如何通知"，不是三套队列：

| backend | 类位置 | `submit_task` 行为 | 使用者 |
|---|---|---|---|
| `BrokerTaskBackend` | task_backend.py:153 | 对已有行补写 `task_payload`（`WHERE ... AND task_payload IS NULL` 防覆盖，228-238；兼容路径，见下）；无 operation_id 时自行 INSERT（243-255） | API 进程默认（memory_engine.py:3008，由 backend 决定） |
| `WorkerTaskBackend` | task_backend.py:126-150 | **no-op**："row already exists in async_operations; a worker will claim it" | `hindsight-worker` 独立进程（worker/main.py:282-288）——worker 执行中触发的子任务（如 retain 触发的 consolidation）行已入库，交给下一轮轮询，避免阻塞父任务 |
| `SyncTaskBackend` | task_backend.py:95-123 | 立即内联执行（`_execute_task`） | 测试/嵌入用法 |

关键演进点（memory_engine.py:22035-22038 注释）：早期 INSERT 不带 payload、靠 `submit_task` 二次 UPDATE 补——两次写之间崩溃会留下"payload 为 NULL 的行"，而认领查询要求 `task_payload IS NOT NULL`，该行永远无人认领。现在 payload 与行**同一 INSERT 原子写入**（22038-22043 构造、22227-22239 落库），UPDATE 只作兼容。

**提交期去重**（`_submit_async_operation`，memory_engine.py:21980-22244）：retain 之外的异步提交都走这个通用入口，两级去重避免重复任务堆积——`dedupe_by_bank`（22082-22117）按 bank+operation_type 查 pending（可选含 processing）行，命中则复用既有 operation_id 直接返回 `deduplicated=True`，适用于"一个 bank 同时只该有一个"的作业（如手动 consolidate）；`dedupe_in_flight_payload_key`（22180-22223）更细，按 `task_payload->>'<key>'` 匹配同一负载主体（如同一 `mental_model_id`）的在途任务。去重开启时提交会先对 banks 行加 `FOR NO KEY UPDATE` 锁（22049-22081，注释从 22050 起给出"为何不是 FOR UPDATE"的 FK FOR KEY SHARE 冲突分析，clause 按 `serialize` 条件拼接在 22077-22080），把"查重→插入"串行化，防两个并发提交都看不到对方（#1842）。

## 3.2 认领 SQL 的核心逻辑与公平性

`claim_tasks`（ops_postgresql.py:1879-1952）两阶段：Phase 1 按预留池逐类型认领（consolidation 有专属路径与 bank 优先级分层，`_claim_consolidation_tasks`，ops_postgresql.py:1549-1630）；Phase 2 认领共享池——2a 非 consolidation 走 rot/fifo 一条语句，2b consolidation 补位。认领完统一 `mark_operations_processing`（ops_postgresql.py:1954-1972）。

**公平性是三层旋转的叠加**：

1. **租户层轮转**（poller.py:605-691）：pass 1 只扫"有活"的 schema、每个池每 schema 至多认领 1 条；pass 2 才向有活的 schema 回填剩余槽位。`_next_schema_idx` 指针在轮末推进到"最后服务的 schema 之后"，让忙租户不能永远排在队首。
2. **bank 层轮转**（rot/fifo，ops_postgresql.py:1784-1828 `_claim_shared_tasks` docstring；`_claim_reserved_tasks` 在 1756）：全局 FIFO 会让一个正在批量灌入库的 bank 占满所有槽、别的 bank 排队（#3861）。`rot` CTE 是"bank_id 严格大于游标的第一行"（tier 0），`fifo` 兜底补满（tier 1）；游标 `_next_bank_cursor` 从结果里免费带回（claim_tasks:1930），空 rot 层即游标到头自动重开。单 bank 部署只多付一次空 seek。注释给出实测：5 万 pending 行时 rot seek 0.10ms vs 裸 FIFO 0.02ms，且轮转与 FIFO 合并成一条语句、无额外往返。
3. **bank 串行化谓词**（`bank_serialization_sql`，engine/db/ops.py:157-240）：每个查询都带这个谓词，保证**同一 bank 的同类作业（consolidation/graph_maintenance）全局至多一个 in-flight**——两个并发 consolidation 会读到同一批未整合记忆并重复喂给 LLM（#3700）。谓词有两个分支：`processing` 分支禁止与在跑的作业并发；"严格更老的 pending"分支防止一个批次的认领把同 bank 的多行全拿走（含"对端已认领未提交"的窗口）。它是**谓词而非独立认领阶段**，理由写在注释里：graph_maintenance 没有预留槽（默认 0），fairness pass 只给 `shared_limit=1`，若做成独立阶段会被一条排队的 retain 无限期饿死。

**重查为什么必要**：外层查询在 `FOR UPDATE` 下重申全部谓词（1815-1819 注释）——CTE 读行时未加锁，两个 worker 并发时，被对端先锁住的行会在重查时落选而不是被认领两次。代价是 SKIP LOCKED 偶尔让本轮少拿几行，下一轮自愈。

## 3.3 失败恢复与重试

执行期的五条出路（`_execute_task_inner`，poller.py:1232-1368；0.10.3 起**所有终态写库都经 `_write_terminal` 包装**，见下）：

| 异常 | 处理 | 写库效果 |
|---|---|---|
| 正常完成 | `_mark_all_completed`（fold 的全部 operation_id 一起终态，v0.10.2 起同时置 `error_message=NULL` 清掉陈旧错误，#4880）；调用点特意放在 `else` 子句（try/except 域之外）——工作已经跑完，completed 写失败绝不能被改判成 failed | `status='completed'` |
| `_WallTimeoutExceeded` | 失败 + 引擎回调 `on_task_wall_timeout`（补发 consolidation 失败 webhook） | `status='failed'` + 可读原因（含 stage 与 env 变量名） |
| `DeferOperation` | `_defer_all` → `_defer_operation`（poller.py:1094-1126） | 回 pending + `next_retry_at`，**不**加 retry_count、不写 error_message（"intentional backpressure, not a failure"）；0.10.3 起 UPDATE 同时把 `metadata` 合并进 `result_metadata`（`COALESCE(result_metadata,'{}') \|\| $3::jsonb`）——反复 defer 的操作由此保持可见 |
| `RetryTaskAt` | `_schedule_retry`（poller.py:1046-1073） | 回 pending + `next_retry_at` + `retry_count+1` + `worker_id=NULL, claimed_at=NULL`；并把本次错误存进 `result_metadata.last_retry_error`——完成时会清 error_message，重试历史改由这里保留（#4858） |
| 其他异常 | 先判 `is_store_backpressure`（存储端因索引落后而甩负载→defer，不烧重试预算）；再判 `is_append_conflict`（0.10.3 新增 #5396，append 输了竞态→带退避的 defer，见下）；否则 `_mark_all_failed` | `status='failed'` 或回 pending（defer） |

**backpressure：两个"该等而非该败"的判定**（`worker/backpressure.py`；`is_store_backpressure` 自 0.10.2 已有，0.10.3 的 #5396 加入 `is_append_conflict` 并把两者统一成这个框架）。模块 docstring 给出的动机：worker 给失败任务的尝试次数很少、总共只有几分钟，一次大摄入若 store 的索引积压超过这个窗口才能排干，任务会在 store 仍在正常甩负载时**耗尽重试被永久标记 failed**——大语料导入就此丢掉积压越界那一刻恰好在途的文档，即**整次运行的尾部静默消失**，只留下一行读起来像"内容有错"的 failed。所以两类错误都分类为背压、走 `DeferOperation`（"not yet, try later"，不加 retry_count）：

- `is_store_backpressure`：**按消息文本匹配**（`_BACKPRESSURE_MARKERS`：`fold behind`/`write bound`/`writes are shed`/`StatusCode.UNAVAILABLE`，backpressure.py:33-38）。不按类型匹配的理由写在 docstring：store 说 gRPC、provider 把 `AioRpcError` 原样重抛，没有可 catch 的共享异常类，发明一个意味着与 store 客户端协同改代码；这些串就是 store 自己的拒绝文案，且**误报的代价只是一次 defer**——真坏掉的任务下次重试时消息不再匹配、照常失败。
- `is_append_conflict`：**按异常类型匹配**（`ConcurrentAppendConflict`/`StoreWriteConflict`，backpressure.py:58-81）——与 store 的 gRPC 拒绝不同，这两个是自己的类。append 输掉竞态是"文档动过了，重读一遍"：前置条件整体拒绝写入、什么都没落库，重做永远安全；在这里判失败就是**丢掉它携带的那一轮对话**（issue #5393）。
- 两个判定都沿 `__cause__`/`__context__` 链下钻——provider 会先包一层，标志通常不在最外层异常上。
- append 冲突的 defer 带抖动指数退避（poller.py:52-64）：首次 2s、逐次翻倍、上限 300s，乘 `uniform(0.5, 1.5)` 抖动防同时输家再次对齐相撞；**刻意不设次数上限**——无限 defer 表现为一行变老的 pending 操作加持续上涨的 defer 计数（可恢复），上限则是丢一轮。defer 次数记在 `result_metadata.append_conflict_defers`（`_append_conflict_defer_count` 读取，读失败回落 0 而非失败任务，poller.py:1075-1092），日志带 bank 与文档清单。
- 引擎侧并非直接把竞态交给 worker：管线内 `_retain_batch_with_append_retry` 先抖动重试 3 次（02 篇 3.11），0.10.3 起会话提交层的 `StoreWriteConflict` 也走同样的原地重做（深拷贝原始提交、新会话重来、丢弃未完成尝试的 outbox 事件，`memory_engine.py:7338-7406`）——到达 worker 意味着争用活过了这两层，延后再试通常就能清掉。

**终态写库本身也可能失败**（`_write_terminal`，poller.py:1369-1405，0.10.3 #5377/#5380）：终态 UPDATE 是一次 DB 调用，DB 不健康（池耗尽、连接重置、主库故障切换）时它一样失败——放弃会把行留在"worker 还活着却已忘掉它"的 processing（#3228），而旧的两条兜底各有缺陷：#3228 时代失败 failed 写库时的一次性 requeue 撞的是同一个死库，completed 写失败则会落进失败路径、把跑完的工作记成 failed。现在改为**限期重试**（首延迟 1s、指数退避、上限 30s，总期限 `TERMINAL_WRITE_DEADLINE_S = 300s`——长到能骑过一次 Postgres 主库故障切换），期限已过仍不可达则**worker 自杀**（`_exit_process`，poller.py:1407-1412，`os.kill(pid, SIGTERM)`）：重启后 `recover_own_tasks` 把行还回 pending。重入队列本身也是错的目标——它会重跑已经跑过（且未必幂等）的工作。

**崩溃恢复的三条线**（收口在 `_reclaim_own_processing_tasks`，poller.py:1414-1498）：

- 启动时 `recover_own_tasks`（poller.py:1500-1552）。0.10.3 起（#5411，issue #5407）**恢复与轮询并行**：`run()`（1800-1827）先读一次 DB 时钟（`SELECT now() AT TIME ZONE 'UTC'`，1809-1817），把 `recover_own_tasks(claimed_before=started_at)` 作为后台 task 拉起（1822）、随即进入 `_poll_loop`——此前恢复在轮询之前 await，数万租户 schema 的扫描要几分钟，重启要等它走完才开始抢单。`claimed_before` 贯穿到 `_recover_batch_operations` 与 `_reclaim_own_processing_tasks` 的谓词（`claimed_at < $1`），恢复只碰**启动之前**认领的行，与本轮轮询新认领的行无竞态；Oracle 读回的是 `SYS_EXTRACT_UTC(SYSTIMESTAMP)` 的 naive UTC 时间戳（方言层为此新增 `NOW() AT TIME ZONE 'UTC'` 重写，db/oracle.py:440，#5437——直译会是带时区名的时间戳、thin 驱动解不出 DPY-3022），补上 UTC tzinfo 后与认领行比较。长扫描每 1000 个 schema 打一条进度日志（`_RECOVERY_PROGRESS_EVERY`，139 行），结尾汇报耗时。恢复内容不变：把**本 worker_id**名下 `processing` 的行——低于重试上限的回 pending（`retry_count+1`）、达到上限的置 failed（防"认领→磨死→回收"死循环，#2675/#2834）——外加 batch API 行恢复（`_recover_batch_operations`，1591-1668，按 `result_metadata->>'batch_id'` 识别；其 UPDATE 现在带 `AND status='processing'` + `claimed_at` 截止的双重复查，防另一个重启中的 worker 已重置并重新认领）与孤儿 parent 收敛（`_reconcile_orphaned_parents`，1671-1798）。**孤儿 parent** 指 batch_retain 父行在子件已全部终态后仍卡在 pending/processing——崩溃发生在子件终态提交与父行更新之间，或子件压根没提交，状态图里 `pending --> cancelled` 的"清理"即指此收敛。0.10.3 起（#5178）该 pass 改为**预过滤查询 + 行锁下复查**：`fetch_reconcilable_batch_parents`（engine/db/ops.py:937）一条 SQL 找出"无未完结同 bank 子件"的候选父行，只有候选才逐个锁行复查——替换掉原来每个 pending parent 一个事务的实现（51k 父行积压时启动要 7m46s）；发现查询只是预筛不是裁决，可能交回其实健康的父行（锁下复查放行），但绝不漏掉该修的。Oracle 侧配套修了 `result_metadata @> :'{"parent_operation_id":...}'` 的方言重写（#5164）：通用 JSON_EXISTS 形式声明了一个 '$' 路径从不引用的绑定变量、Oracle 对每行求值为 false——batch_retain 父行在 Oracle 上根本找不到子件、永远 pending。
- 关停时 `release_own_tasks`（poller.py:1554-1588）：drain 超时后把仍归自己的行还回 pending。动机写在注释里：默认 worker_id 派生自 hostname，容器里**永不复现**，没有这条路径那些行会永远卡在 processing（#3228）。`stop()` 里还会先取消仍在走 schema 的启动恢复 task（1959-1964），防它与下面的 release 竞态。
- 运行中兜底：终态写库失败由 `_write_terminal` 的限期重试接住（见上），不再有单行抢救路径。

**wall-clock 上限**（poller.py:76-112）：retain 类是**绝对**上限（一个文档跑一小时必有蹊跷）；consolidation 是**空闲**上限——每个提交批次推进 stage 都会 `asyncio.timeout(...).reschedule()` 重置时钟（1173-1230 内，reschedule 在 1197-1202），只有真正停滞才触发。`refresh_mental_model` 在 v0.10.2 也拿到了显式上限（借用 reflect 的 `reflect_wall_timeout`，poller.py:110-112）——没有上限的卡死刷新会一直占着 worker 槽直到重启（#4581）。触发时取消执行器，因此引擎自己的 except 块（会发 webhook）不会跑，poller 用 `on_wall_timeout` 回调代为通知（`_notify_wall_timeout`，1217 起）。设计动机：卡死的任务会永占 worker 槽、操作停在 API 既不重试也不可取消的 `processing`（#3002）。

**worker 与 API 的关系总结**：同一张队列表、同一份认领代码（`WorkerPoller`）。默认 API 进程内嵌一个 poller（standalone）；`hindsight-worker` 是同构的独立进程，区别仅在 task backend 用 no-op 的 `WorkerTaskBackend`、且 `run_migrations=False`（worker/main.py:282-288），启动前会检查 `supports_worker_poller`（303 起，不支持的 backend 直接退出而非悄悄同步执行）。`--max-retries`/`--log-level` 两个 CLI 旗标已退役（0.10.3 #5190）：它们从来只改启动 banner、够不着 poller/engine/日志——仍被接受以免既有启动命令失效，但会向 stderr 打印"set HINDSIGHT_API_WORKER_MAX_RETRIES / HINDSIGHT_API_LOG_LEVEL instead"（worker/main.py:207-216）。`hindsight-admin` 另有 `decommission-worker(s)`/`worker-status` 命令按 worker_id 清理遗留行（admin/cli.py:1458-1518 附近）。

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
- **什么条件下拒绝**：lane 启用（`max_in_flight > 0`）且请求在队列里等了超过 `max_wait_seconds` → `AdmissionRejected` → handler 依赖层转 **503 + `Retry-After`**（http.py:5517-5522）。默认值（config.py:1577-1602）：recall 16/核、等 30s；reflect 16/核、等 5s；retain 32/核、等 2s。`max_in_flight` 环境变量为**负数**才关闭 lane（0 表示"按核数推导"）；`max_wait_seconds=0` 是"绝不排队"模式——内部仍走 acquire，只是 deadline 设成 0.001s（admission.py:215），继续走同一条 acquire 路径，统计不失真。
- **等待是可中断的**：`_acquire_unless_abandoned`（109-152）用 `asyncio.wait` 让"拿许可"与"断连令牌"赛跑；客户端先走则抛 `_ClientGone` → **499** 关闭（http.py:5513-5516），既不占槽也不造响应。取消 pending acquire 后若许可恰好已到手，会立刻 `semaphore.release()` 归还防泄漏（142-148）。
- **为什么按操作分 lane 而不是全局限流**：同一 API 上操作成本跨三个量级（/health/live 约 0.1ms、bank stats 约 0.5ms、recall 约 23ms，admission.py:26-31）——按 recall 校准的全局限流会掐死健康检查。
- **限额是每 worker 进程一份**（admission.py:33-35）：`--workers N` 时全局预算 = N × max_in_flight。main.py:463-466 把 N 写回环境变量，让子进程各自分摊 CPU 预算。
- 拒绝点在 FastAPI 依赖里，**body 反序列化之前**——最便宜的拒绝位置（http.py:5533-5535）。

## 3.5 可观测性与运行时安全

**metrics（metrics.py，1346 行）**：OpenTelemetry API + Prometheus reader，`/metrics` 暴露。核心 instruments（metrics.py:456-633）：`hindsight.operation.duration/.total`（取消的请求**不计入** total——success/failure 都不算，issue #2122，metrics.py:66-88 沿 `__cause__` 链识别 `OperationCancelledError`）、`hindsight.llm.duration/tokens.*`（v0.10.2 起推理令牌单独入账，`tokens.cached_input`/`tokens.thoughts`）、`hindsight.http.duration/.requests.total/.in_progress`、`hindsight.db.pool.acquire_wait` + size/idle/min/max/**waiting**（waiting 需要池侧 instrumentation 提供 asyncpg 不暴露的等待者计数）、recall/retain/validator 分阶段直方图、`hindsight.event_loop.stalls/stall_duration/lag`。

**HTTP 指标的 `endpoint` 标签改用路由模板（0.10.3，#5063）**：旧法（#2191）在 `metrics.py` 里用三条正则把原始路径的 bank id 段/UUID/数字段折叠成占位符（`normalize_http_endpoint`，已删除）——其余一切路径参数原样漏过：document id（`:path`、可跨段、可为任意形态）、memory/entity/operation id、文件 key，每个 id 一条**永不淘汰**的 OTel 序列。现在 `http_endpoint_label`（`api/observability.py:93-117`）按 Starlette 路由器的方式走一遍（展平后的）路由表：第一个 FULL match 返回其 `path_format`（converter 剥离后的模板），否则第一个 PARTIAL match（即 405），全不中则返回常量 `unmatched`（扫描器 404、尾斜杠重定向——一个常量而非客户端完全可控的路径）；命中无模板的路由（Host 路由等）用 `unknown_route`。基数由路由数量界定，且与 trace 的 `http.route` 同源（OTel FastAPI instrumentation 做的就是这次查找）。两个实现细节：FastAPI ≥0.137 的 `include_router` 不再把子路由拷进 app 的路由表、而是挂一个没有路径模板的 `_IncludedRouter` 节点，须用 `iter_route_contexts()` 展平（`_flatten_routes`，observability.py:73-84）；标签在**请求运行前**解析而非事后读 `scope["route"]`——in-progress 上下计数器必须在它稍后被扣减时的同一标签下递增。bank_id/tenant 标签默认关闭、token 桶化不变。可选 backlog gauge（默认关，`HINDSIGHT_API_METRICS_BACKLOG_ENABLED`）由 30s 后台循环跨所有 schema COUNT `async_operations`（metrics.py:1133 起 `_setup_backlog_metrics`）。

**/metrics 的渲染已移出事件循环（#4617）**：`generate_latest()`（多 worker 模式下还有 `WorkerMetrics.render()`，带文件 I/O）是同步的，序列化成本随基数增长，大注册表上要几百 ms 到秒级；以前内联 await 会把 asyncio 循环整个冻住——/health、WebSocket 握手、该 worker 在处理的一切请求都停摆。现在 `await asyncio.to_thread(render)`（http.py:5736-5737）；worker 进程的 `/metrics` 同样处理（worker/main.py:149-155——它的 app 还兼着 `/health/live`，卡住的探活会触发重启并连坐已认领任务）。移出线程不等于免费：纯 Python 渲染握着 GIL，但循环从"全程冻结"变成按 5ms 时间片让出，是"降级"与"假死"的区别。

**`memories_backend` 标签（#5129，opt-in）**：混合存储部署（不同 bank 归不同 memories store）需要比较各后端延迟，而 per-tenant 标签基数太高。`MemoriesExtension.backend_name_for(bank_id)`（memories/base.py:1112，默认返回空串=不加标签，已有序列保持不变）给出 store 名后：`hindsight.operation.*` 挂 `memories_backend`（每次操作解析一次，metrics.py:694-695）；operation 内记录的 recall 分阶段经 ContextVar `_current_memories_backend`（metrics.py:47）继承同一标签，无需把 bank_id 传进每个阶段调用点；retain 阶段的 `store` 标签优先用这个名字（retain/timing.py:187-192 的 `timed_retain`）。

**多 worker 指标（metrics_multiworker.py）**：`--workers N` 时一次抓取只会打到随机一个 worker。方案不是 OTel 多进程模式（PrometheusMetricReader 跨进程不可合并），而是：每个 worker 用非阻塞 `flock` 认领槽位 0..N-1（内核在进程死亡时自动放锁，重启 worker 复用槽号、标签有界）→ 守护线程每 5s 原子写 `worker-<slot>.prom` 快照到以 supervisor pid 命名的临时目录 → `/metrics` 把自己 + 15s 内新鲜的其他槽快照合并，所有样本贴 `api_worker="<slot>"` 标签，**不求和**（"summing is a query-time decision"，metrics_multiworker.py:96-98）。开关 `HINDSIGHT_API_METRICS_WORKER_LABEL`（默认 false）。

**tracing（tracing.py）**：OTLP HTTP exporter，`HINDSIGHT_API_OTEL_TRACES_ENABLED` + `HINDSIGHT_API_OTEL_EXPORTER_OTLP_ENDPOINT`（默认均关）。span 层级：HTTP server span 按操作**改名**（`hindsight.recall/retain/reflect/...`，`_name_server_span_after_operation` http.py:5347-5376，让 trace 标题是操作而非 URL 模板）→ engine 的 `hindsight.*` 父 span → GenAI 语义规范的 LLM 子 span（prompt/completion 以事件附带，内容截断 10 万字符）→ reflect 的 per-tool span。跨进程续 trace：入队时把 W3C traceparent 注进 payload 的 `_traceparent` 键，worker 的 `execute_task` 解出并 `otel_context.attach`（memory_engine.py:4614-4633）。

**liveness vs readiness（liveness.py + 路由）**：`/health/live` 只回答"进程是否 wedged"，**永不触库**（模块 docstring：一个 `SELECT 1` 存活探针会把数据库退化放大成全量重启风暴——所有 pod 同时被杀，已认领任务集体重新入队冲向失败悬崖）；`/health`/`/health/ready` 查 `memory.health_check()`，不健康回 503，用于**挡流量**而非重启进程。worker 侧同款三分法，外加 `seconds_since_last_poll` 仅供告警、永不影响状态码。

**loop_watchdog（独立线程）**：worker 和 API 共用一个事件循环，同步调用卡住循环时 `/health` 无法调度。看门狗运行在**另一个 OS 线程**（"基于协程的监视器会被它想观察的卡顿本身冻住"），每 250ms `loop.call_soon_threadsafe` 投一个 ping，1s 内未被服务就 `sys._current_frames()` 抓循环线程栈打 `[EVENT LOOP BLOCKED]` 告警并计 `hindsight.event_loop.stalls`——只观察、不动循环，uvloop 兼容（loop_watchdog.py:9-16, 107-163）。

**loop_lag（进程内探针）**：每 50ms tick 测"睡过头"多少，采样进 `hindsight.event_loop.lag` 直方图并按窗口打 p50/p90/p99 日志；动机：分阶段计时器只覆盖自己 await 的部分，"可运行却没在跑"的协程不可见（实测分阶段计时只覆盖 recall 墙钟的 10%，loop_lag.py:1-13）。默认关闭。

**cancellation（三跳传播，cancellation.py + api/disconnect.py）**：① `ClientDisconnectCancellationMiddleware`（纯 ASGI，装在 BaseHTTPMiddleware 之外，因为后者的内存流会让 `is_disconnected()` 永远不触发，#2122）在 `/memories/recall`、`/reflect` 以及 **`POST /memories`**（0.10.3 起 #5358，方法判定防误伤同路径的 DELETE；`_should_monitor`，disconnect.py:51-62）上监听原始 `receive` 的 `http.disconnect`；② 令牌挂 `scope["hindsight.cancellation_token"]`，recall/reflect 的 handler 经 `run_cancellable_on_disconnect` 注入 `RequestContext.cancellation`，engine 在**阶段边界**轮询 `raise_if_cancelled()`（已进入 executor 线程的图扩展/重排计算无法被取消——协作式设计的边界）；**同步 retain 走另一条路**——`run_task_cancellable_on_disconnect`（http.py:404-451，0.10.3 #5358）把整个执行作为 task 与断连令牌赛跑，客户端先走就 `task.cancel()` 并**等待收尾**（unwinding 的任务仍会提交部分工作、拆自己的子任务，#5372 的 finally 兜底让它安全）；③ 捕获 `OperationCancelledError`/取消转 HTTP 499。`OperationCancelledError` 刻意继承 `Exception` 而非 BaseException——引擎里大量 `except Exception` 需要显式重抛它，且 `gather(return_exceptions=True)` 的结果分类依赖 isinstance（cancellation.py:25-40）。

**profiling 与 http_probe**：`HINDSIGHT_API_PROFILE`（JSON）驱动进程级 cProfile 周期性把**窗口增量**打进日志流（理由：`kubectl logs --previous` 比 file 活得久）；v0.10.2 起 `create_app` 也装一次 profiling（http.py:5069-5073）——`--workers N` 时 uvicorn 子进程 import app 但从不跑 `main()`，只在 main 装会 profile 到什么也不干的 supervisor；`http_probe.py` 是 stdlib-only 的探活小工具（禁止 import 引擎/三方包，有测试守卫），替换掉运行时镜像里的 curl（消 9 个 HIGH CVE）。

## 3.6 扩展槽位：6 个插口、一条加载协议

加载协议（`hindsight_api/extensions/loader.py:72` 起）：读 `HINDSIGHT_API_{PREFIX}_EXTENSION` 环境变量 → 值为 `"module.path:ClassName"` → importlib 导入并校验 `issubclass(base_class)` → 把**所有** `HINDSIGHT_API_{PREFIX}_*` 环境变量（去掉 `_EXTENSION` 本身）剥前缀小写成 config dict → 实例化并 `set_context`。加载点分布：TENANT/OPERATION_VALIDATOR 在四个入口各自加载（main.py:367-375、server.py:58-65、worker/main.py:267-277、admin/cli.py:308）；HTTP 在 create_http_app（http.py:5077-5082）；MCP 在 `create_mcp_server`（mcp.py:199-203）；MEMORY_DEFENSE 与 MEMORIES 由 engine 自己加载兜底（memory_engine.py:3098-3126、engine/memories/__init__.py:47-59）。

| 槽位 | 抽象类（file:line） | 钩子 | 官方实现 |
|---|---|---|---|
| TENANT | `TenantExtension`（extensions/tenant.py:54） | `authenticate`（返回 TenantContext.schema_name）、`authenticate_mcp`、`list_tenants`（worker 轮询/迁移扇出）、`get_tenant_config`/`get_allowed_config_fields`（层级配置与写权限）、`extra_bank_tables`/`provision_bank_tables` | 内置 `DefaultTenantExtension`（无鉴权）、`ApiKeyTenantExtension`（单共享密钥）；registry 包 `static-keys-tenant`（env 声明 user:key 对→每用户 schema）、`supabase-tenant`（JWKS 验 JWT） |
| OPERATION_VALIDATOR | `OperationValidatorExtension`（extensions/operation_validator.py:637） | `precheck`（8 条计费路由、body 未读）；`validate_retain/recall/reflect/create_bank`；`validate_bank_read`/`validate_bank_write`（engine 内分别 22/42 个调用点，枚举面 `BankReadOperation` 32 / `BankWriteOperation` 40 个操作名；0.10.3 起 `import_documents` 也过 `validate_bank_write`——`_ensure_bank_exists` 只对缺 bank 的导入跑 `validate_create_bank`，导入进既有 bank 此前完全不触任何 validator 钩子，#5308/#5137）；`on_*_complete`（ret/recall/reflect/... 完成回调，计费计量面）；`filter_bank_list`/`filter_mcp_tools`（只减不增） | 无内置实现（云版计量/配额面）；`validate_consolidate`/`on_consolidate_complete` 在 api-slim 内无调用点 |
| HTTP | `HttpExtension`（extensions/http.py:19） | `get_router`（挂 `/ext`）、`get_root_router`（挂根）、生命周期钩子 | 无 |
| MCP | `MCPExtension`（extensions/mcp.py:21） | `register_tools(mcp, memory)` 追加工具 | 无 |
| MEMORY_DEFENSE | `MemoryDefenseExtension`（extensions/memory_defense.py:336） | `screen(policy, bank_id, document_id, content, tags)` → allow/redact/block | 内置 `MemoryDefenseRegexExtension`（约 40 模式的密钥/PII 目录 + Luhn 卡号校验） |
| MEMORIES | `MemoriesExtension`（engine/memories/base.py:1048） | 整个存储+检索面（写入路径、recall 各臂、寻址读、维护），`store_owned` 极性标志 | 默认 `PostgresMemories`（v0.10.2 起实现拆在 `engine/memories/pg/` 包内，#4969/#4979） |

实例化后的全程装配：`MemoryEngine.__init__`（memory_engine.py:2489 起）给 validator 套计时 instrumentation（validator_instrumentation.py:66，逐钩子记 `hindsight.validator.phase.duration`）、给 tenant/validator 补 `set_context`（它们在 engine 之前构造）、构建 `DefaultExtensionContext`（给扩展受控的 `run_migration`/`get_memory_engine` API；基类 `ExtensionContext` 在 context.py:11，默认实现 `DefaultExtensionContext` 在 context.py:76）。memory defense 在 retain 编排器里逐内容项执行（engine/retain/orchestrator.py:1640 起）：REDACT 会重写内容对象与原始 dict（落库前），BLOCK 累积违规，全阻断 → `MemoryDefenseAllBlockedError` → HTTP 422。

## 3.7 bank 隔离在 API 层的体现

- **HTTP 面**：所有业务路由的 `{bank_id}` 是显式路径参数；请求进入 engine 前第一个引擎级动作是 `_authenticate_tenant`（memory_engine.py:3521-3568）：`request_context.internal`（后台/worker 任务）与 `mcp_authenticated`（传输层已验）直接复用当前 schema，否则调 `tenant_extension.authenticate` 并把 `_current_schema` ContextVar 设为租户 schema——之后**所有** SQL 通过 `fq_table` 按此 contextvar 加 schema 前缀。v0.10.2 起鉴权结果还缓存在 `request_context.authenticated_schema` 上：别名解析（route class）、`precheck_for`、端点自身调用会连续触发鉴权，缓存让第二个及以后的调用直接复用 schema，省掉重复的租户查询（复用分支 3559-3561，鉴权成功后回写缓存 3567，注释 3554-3558）。engine 内该鉴权调用有 95 处（每个公开引擎方法开头）。
- **MCP 面**：bank_id 解析链 = 路径段（单 bank 模式）> `X-Bank-Id` 头 > `HINDSIGHT_MCP_BANK_ID`（默认 "default"）；鉴权成功后 `_current_schema.set(tenant_context.schema_name)`（mcp.py:490），随后把 bank_id/租户/密钥/额外头全部放入 ContextVar（mcp.py:545-556），工具执行时经 resolver 组装 `RequestContext`。
- **Worker 面**：租户扩展的 `list_tenants()` 每轮轮询动态发现 schema（poller.py:443-447）；认领的行带着 `schema` 上下文，执行前 `task.task_dict["_schema"] = task.schema`（poller.py:1274），engine 的 `_execute_task` 弹出并设 ContextVar（memory_engine.py:4640-4642）。worker/main.py:268-272 注释警示：租户扩展必须在建 engine 之前加载，否则 `_authenticate_tenant` 会把 schema 重置回 "public"，worker 写入落错 schema。
- **存在性守卫与缓存 TTL（#5432）**：读端点不回答从未创建的 bank（`_require_bank_exists`，200 的空计数与健康的空 bank 无法区分，#4175），但 profile 行缓存在**每进程**的 `bank_info_cache` 里、`delete_bank` 只失效服务它的那个进程——TTL 内另一个进程会把对已删 bank 的请求放行到 store，而 store 的存储已被删除动作清掉，调用在第一条触库的语句上失败。0.10.3 的修法是把"bank 没了"的追问下沉到**唯一看得见所有这类失败的地方**：`raise_if_bank_gone`（engine/memories/__init__.py:85-96）由 store 在"一次调用发现该 bank 无存储"后调用，经引擎启动时装上的 `_raise_if_bank_deleted`（memory_engine.py:5744-5748，`set_bank_gone_check`；关停时摘除）给出与存在性守卫相同的 404；bank 仍活着则原样重抛 store 自己的错误（活 bank 无存储是真故障）。一次调用成功时零成本，路由、工具与后台工作一齐被覆盖。
- **维护扇出**：迁移（`hindsight-admin run-db-migration` 遍历 base + 全部租户 schema）、备份/恢复、`delete_bank` 的扩展表清扫（`extra_bank_tables` 声明 `BankScopedTable` 描述符）都沿租户列表循环。
- bank 间无交叉查询；bank 级调度公平（3.2 的 bank 轮转）与 bank 级串行化（bank_serialization_sql）都在 SQL 谓词层实现，而非进程锁。

## 3.8 生产部署形态与多副本注意

- **单进程能跑**：`hindsight-api` + `HINDSIGHT_API_DATABASE_URL=pg0://...`（embedded Postgres，需 `embedded-db` extra，且要求 `pg0-embedded>=0.15.2`——0.10.2 后期 #5194 抬高的最低版本）即全家桶——REST、MCP、poller、迁移（`run_migrations_on_startup` 默认开）都在一个进程。Docker standalone 镜像即此形态（外加同容器的控制面 node 进程）。
- **多副本要留意**：
  1. 迁移幂等，多个 uvicorn child 各自 import `server.py` 时都会跑一遍（server.py:69 注释明说 safe）；多副本 API 无共享内存状态（准入/信号量均为进程内 asyncio 对象），可横向扩。分布式互斥靠 schema 级 advisory lock（migrations.py:535，`pg_try_advisory_lock` 轮询而非阻塞——持锁连接不能留未提交事务，否则 CREATE INDEX CONCURRENTLY 死锁）；v0.10.2 起迁移失败也会释放锁：先回滚中止的事务再 `pg_advisory_unlock`、解锁失败只记日志不吞原始错误，等待方现在会周期性打出持锁者信息（pg_locks + pg_stat_activity）而非无声挂起（#4623/#4611）。
  2. 准入限额按进程计（admission.py:33-35），容量规划按 `副本数 × 每进程限额`。
  3. `--workers N` 的 `/metrics` 需要 `HINDSIGHT_API_METRICS_WORKER_LABEL=true` 才能看到全部 worker 的序列（3.5）。
  4. worker 拆分：API 侧 `HINDSIGHT_API_WORKER_ENABLED=false` + 独立 `hindsight-worker`（helm 默认 worker.enabled=false，`values.yaml:136-138`，副本数 2）；worker_id 默认取 hostname，StatefulSet 用稳定 pod 名显式注入（worker-statefulset.yaml:62-66），容器里随机 hostname 会导致崩溃后"自己的行没人认领"（#3228，由 shutdown release + admin decommission 兜底）。
  5. 槽位是每 worker 进程的：`max_slots=10`、预留 `{consolidation: 2}` 是**单进程**值；扩 worker 数即可扩总并发。
  6. 超时族：`timeout_graceful_shutdown=5`（HTTP）、`shutdown_graceful(30s)`（poller drain，worker/main.py:404）、retain/consolidation wall ceiling、池获取侧两级慢阈值——`HINDSIGHT_API_DB_ACQUIRE_WARN_THRESHOLD_MS`（默认 1000ms，喂给池 instrumentation 的 acquire 计时告警，engine/db/postgresql.py:266）与 `HINDSIGHT_API_DB_POOL_SLOW_ACQUIRE_THRESHOLD_SECONDS`（默认 0.05s、0 关闭，0.10.3 起 #5397 可配）：后者驱动 `acquire_with_retry` 的 `[DB POOL] Slow acquire` 日志（engine/db_utils.py:116-122 的 `_is_slow_acquire`——读缓存版 `_get_raw_config()` 而非 `get_config()`，后者每次构造代理约 0.3ms，而这跑在**每一次**取连接上）。分层关停先 drain 任务、再归还行、再刷 tracing span（worker/main.py:394-428）。

## 3.9 Bank 可携性：transfer/ 子系统与 bank 别名（v0.10.2 头部特性）

v0.10.2 把"搬一个 bank"收敛成一套词汇：**一个归档格式**（transfer ZIP，`engine/transfer/`）、**一个提交面**（`POST transfer/export`、`POST transfer/import`、`POST clone`，全部 202 异步 + operation_id 轮询）、**一个存储面**（FileStorage 流式读写）。归档里只有事实的**文本形态**：embedding 与 DB id 永不携带，导入端用目标 bank 的 embedding 模型重嵌、重解析实体、按目标 bank 现有记忆重建链接——不调 LLM，所以导入零 token 成本、不发明新事实。

```mermaid
flowchart LR
    subgraph SRC["源 bank"]
        S1["documents / memory_units<br/>文本为权威"]
        S2["observations 与 mental_models<br/>与 knowledge_pages"]
        S3["attachments 字节<br/>在 file storage"]
        S4["banks 行 + directives + webhooks"]
        S5["audit_log 与 llm_requests"]
    end
    S1 -->|"分批读，去 embedding 与 DB id"| ZIP
    S2 -->|"scope.data"| ZIP["stream_export_bank<br/>ZipStreamer 流式出 ZIP"]
    S3 -->|"retrieve_stream 逐 blob"| ZIP
    S4 -->|"scope.bank_config"| ZIP
    S5 -->|"scope.history 默认关"| ZIP
    ZIP ==>|"async for chunk"| FS["store_stream<br/>banks/{bank_id}/exports/{uuid}/transfer.zip"]
    FS -->|"result_metadata.download_url"| DL["GET files/download"]
    DL ==>|"调用方搬运"| IM["POST transfer/import"]
    IM --> PB["parse_bank_archive<br/>校验 schema 版本"]
    PB --> REPLAY["确定性 retain 管线回放<br/>重嵌 + 实体重解析 + 重建链接<br/>不调 LLM"]
    REPLAY --> TGT[("目标 bank")]
```

**API 面**（http.py，见 1.4 的 Bank Transfer 组）：`transfer/export`（9020）三个 include 标志（`include_data/include_bank_config/include_history`，默认 true/true/false），传 `document_id` 则退化为文档子集（此时禁 bank 级 section，400）；`transfer/import`（9104）分 `mode=restore`（默认，**目标 bank 必须不存在**——恢复而非合并）与 `mode=merge`（文档并入本 bank，`document_conflict=skip|replace|new-id`；merge 拒绝一切 scope 标志，只吃文档）；`clone`（9210）= 导出+导入背靠背且归档不出进程，受 export/import 两个开关共同把门（任一半关即 404）。旧文档级端点 `document-transfer{,/export}`（8946/8882）保留原形状，同步 `GET`（8852）恒 410。

**归档格式与表分类**（transfer/export.py、schema.py）：每个 bank-scoped 表必须落入三类之一（`test_export_bank_covers_schema` 守卫，export.py:56-58——未来迁移加的表不可能被静默漏出归档）：

- **回放类**（documents/chunks/memory_units/entities/unit_entities/memory_links/entity_cooccurrences/observation_history）：文本随 `TransferDocument` 进 documents.json；实体、链接、共现是派生数据，目标端回放重建，observation_history 因观察 id 会再生而无从挂回（`_REPLAYED_TABLES` 及理由注释 export.py:59-84）。
- **原样携带类**：bank 行（banks/directives/webhooks 随 `bank_config`）；mental_models 与 knowledge_pages 随 **data** 走——合成物跟着被合成的东西：模型的 `based_on` 按 id 直指事实（export.py:88-95）；操作日志/附件行/invalidated_memory_units 进 `data/`；audit_log 与 llm_requests 进 `history/`（仅 `scope.history`）。
- **永不导出**：`embedding`/`search_vector` 派生列（`_DERIVED_COLUMNS`，目标端重算）；`bank_aliases`（路由身份不是 bank 内容——带过去会在同实例抢走源 bank 的流量，export.py:115-122）；`file_storage` 表（blob 以 `blobs/000001.bin` 归档条目旅行，attachments.json 给映射）。

**流式导出（#4689）**：`stream_export_bank`（export.py:497）与 `stream_export_documents`（export.py:289）把 ZIP 作为 `AsyncIterator[bytes]` 产出，直接喂 `FileStorage.store_stream`——大 bank 不再把整个归档攒在内存里（旧实现的多 GB ZIP 触发过容器 OOM）。底层 `ZipStreamer`（stream_archive.py:107-348，约 270 行、只依赖 struct/zlib）手写 PKZIP：**Bit 3（0x0008）Data Descriptor** 让 CRC/尺寸在数据之后补发、全程不 seek——标准 zipfile 需要可 seek 文件，而流式响应、S3 上传、DB 管道都不可 seek；Zip64 透明支持 4GB/65535 项；附件按 DEFLATE level-0 包装（帧自终止，兼容 ZipInputStream/funzip 顺序读者，又不浪费 CPU 重压已压缩图片）；喂压缩器的块一律切到 64KB，压缩输出不随 blob 涨；每约 1MB `asyncio.sleep(0)` 让出事件循环。一致性取舍写在 docstring：导出按 section/批次**借多条短连接**（附件复制多久连接就借多久——长持连接会饿死池子），代价是"多时间点快照"；clone 不同，它复制活 bank 且不出进程，在**单事务**里读源端（memory_engine.py:3867-3876）。

**异步任务化**：`export_bank`/`import_bank`/`clone_bank` 三个 operation_type（memory_engine.py:7746/7815/7879 提交，3689/3753/3826 执行）。export handler 流式写 file storage（storage_key `banks/{bank_id}/exports/{uuid}/transfer.zip`，3728），用 `ByteStreamCounter` 顺带量出 byte_size，`result_metadata` 记 storage_key/download_url/byte_size/filename；import handler 取回归档、恢复、把各组件计数并入 `result_metadata`，最后删归档。submit 侧把归档**先验证再入库**（坏 zip 立刻 400 而不是变成后台任务失败），归档本体不 base64 进操作行——按 storage_key 引用。

**导入语义**（importer.py:961 起 `import_bank`）：
- restore 拒绝已存在的目标 bank；迁移恢复**精确状态**——不发 retain webhook、不触发 consolidation/graph 维护（observations/mental models 按导出原样恢复）。
- 文档按 **50 个/事务**批量写（`_DOCUMENT_BATCH_SIZE`，importer.py:78；此前一文档一事务=一轮 embedding+一轮实体解析+一次 commit，#5091）；observation/mental-model 的整体列表按 128/批切片再喂 provider（`_EMBED_BATCH_SIZE`，importer.py:73，防进程内 provider 峰值内存，#3891）。
- 换 id 恢复（restore 到新 target_bank_id）会全行重写 bank_id，全局唯一列（directives/webhooks/history 的 id、operation_id）drop 掉让目标重发；`internal_id`（仅用于 per-bank 索引命名的全局随机标识）一定 drop，否则同实例再导入时 banks INSERT 被 `ON CONFLICT DO NOTHING` 静默跳过、子件全部撞 FK（importer.py:1034-1042，#3270）。
- Oracle（#4636）：`_restore_rows` 原来靠 information_schema 读列型——Oracle 没有；现在读 `all_tab_columns` + CURRENT_SCHEMA，识别 `timestamp(6) with time zone`，并把 Oracle 折成大写的标识符加引号（importer.py:505-551）。

**崩溃重试（#5091）**：worker 在 import-bank 中途重启会留下"半恢复的目标 bank"，重试直接撞 `target bank already exists`。修复分两半：
1. **记录与存在原子**：恢复在建 banks 行的同一事务里执行 `on_bank_created` 回调，把 `restored_bank_id` 写进操作的 `result_metadata`（importer.py:1074-1075 调用位，memory_engine.py:8540-8546 定义）；
2. **重试只清自己造的孽**：重试时仅当操作的 `restored_bank_id` 与目标一致**且**该 bank 仍在，才删除半成品从零重放；操作未曾创建的 bank 永不触碰（memory_engine.py:8527-8538）。

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant FS as FileStorage
    participant W as Worker
    participant D as PostgreSQL
    C->>A: POST transfer/import（restore）
    A->>FS: 存归档得 storage_key
    A->>D: INSERT import_bank 操作行
    A-->>C: 202 与 operation_id
    W->>D: 认领 import_bank
    W->>FS: retrieve 归档
    W->>D: 事务：建目标 banks 行，同事务写 restored_bank_id
    W->>W: 恢复进行中崩溃重启
    W->>D: 重试认领同一操作
    W->>D: 读 restored_bank_id，与目标一致且 bank 仍在
    W->>D: 删除半成品 bank，从零重放归档
    W->>D: 计数并入 result_metadata，completed
    C->>A: GET operations/{operation_id}
```

**bank 别名（#4706/#4724）**：`bank_id` 是所有 bank-scoped 表的文本外键，改名=全表重写+停机（`hindsight-admin rename-bank`）。别名是零停机替代：`bank_aliases` 表（迁移 `c8d1e4f7a20b`，alias 为 PK 天然防两 bank 抢名；`d4f8b1c6e903` 加 `is_primary`，一个 bank 至多在一个别名下展示）让一个 bank 同时应答多个 id。解析做在两个**入口边**：
- REST：`UnknownParamsRoute.get_route_handler` 在路由已解析、FastAPI 取路径参数**之前**把别名重写回 canonical id（unknown_params.py:133-175）。必须在 engine 之外做：约 337 处 `WHERE bank_id = $1` 从不经过 engine 的 bank 助手；解析失败静默吞掉（解析不了不得把真 bank 变 500，未解析 id 行为与无别名时代完全一致；0.10.3 起未鉴权请求的解析失败降为 debug 日志——鉴权稍后反正会拒掉它们，unknown_params.py:163-166 与 mcp.py:539-541）。旧 id 记进 scope 的 `SCOPE_RESOLVED_ALIAS`（observability.py:48）供日志/追踪辨认调用方发的是哪个 id。
- MCP：连接进入时解析一次（mcp.py:522-545），此后 contextvar 里只有 canonical id。这次查找本身会**重新鉴权**（`resolve_bank_alias` 先跑 `_authenticate_tenant`，memory_engine.py:20472，别名按 schema 存放、schema 来自鉴权）——0.10.2 后期起（#5240）该查找的 `RequestContext` 会带上白名单透传头（`extra_headers=dict(passthrough_headers)`，mcp.py:531-545）：此前靠头鉴权的租户扩展在别名查找里拿不到头、查找抛错，连接退回"按原样使用 id"，真 bank 的别名就此解析失败；透传后查找与工具执行看到同一套头。
- 兼容：别名不随归档导出（见上）；`hindsight-admin rename-bank` 按目录里 bank_id 列名枚举表，rename 天然保留别名。

**模板重导入的幂等（0.10.3，#5404）**：`POST /banks/{bank_id}/import` 重放同一份模板 manifest 时，已存在且**内容未变**的 mental model/directive 直接跳过（`_mental_model_matches_template`/`_directive_matches_template`，http.py:4359-4391；跳过点 4408-4411/4455-4456）——此前每次重放都对每个已存在项走一遍 `update_mental_model`，即一次纯属浪费的 LLM 重生成（#5271）。"未变"的判定镜像更新会写的字段（name/source_query/max_tokens/trigger/非空 tags），两处刻意的不对称：manifest 的 tags 为空表示"保留存储侧现状"、永不计为差异；存储侧 trigger 先过 `MentalModelTrigger.model_validate` 拿到与 manifest 相同的缺省值再比（解析不了的旧 trigger 视为已变）。配套把读取现有模型的 `list_mental_models(detail=...)` 从 `metadata` 升到 `config`（http.py:4273/4334）——不带回 trigger 就没法比。

**迁移面**：本版迁移目录 110→111 个文件，新增 1 个：`c8e2f4a6b1d3`（Oracle-only，`mental_models(content)` 的 CTXSYS.CONTEXT 全文索引，`SYNC (ON COMMIT)` 免维护作业——补齐知识库搜索 BM25 臂在 Oracle 上 `CONTAINS`/`SCORE` 查询缺索引的短板，PG 槽位有意缺席，#4804）。v0.10.2 的 6 个新增（`b8d3f1a6c2e4` mental_models.last_refresh_failed_at、`c8d1e4f7a20b`/`d4f8b1c6e903` bank_aliases 及 is_primary、`7c2e5a9d1f40` entities trgm 索引 bank 前缀、`a6c4e8f1b203` llm_requests 推理 token 列、`e5b1c7d3a902` merge 双头）见前版记录。`async_operations` 本身无列变化。

### 文件存储后端与 store-owned 读取（与 05 篇分工）

`engine/storage/` 是 blob 层（FileStorage：postgresql/s3/gcs/azure/native），后端选型与驱动细节归 05 篇；本篇只记 API/服务视角的三点：
- **流式接口成为一等公民（#4689）**：`FileStorage` 基类新增 `store_stream`/`retrieve_stream`（base.py:37-53/70-77；默认实现 buffer 后转调 store/retrieve）与 `get_size`（base.py:129）；S3/GCS/Azure 共享 `ObstoreFileStorage` 基类（base.py:150 起），`store_stream` 走 `open_writer_async` 分片上传——失败路径放弃 writer 并尽力删半成品，桶侧必须配 `AbortIncompleteMultipartUpload` 生命周期规则清孤儿分片（base.py:176-183 注释）。transfer 是第一个流进流出的客户：导出 `store_stream`，`_stream_attachments` 导出侧逐 blob `retrieve_stream`。
- **store-owned bank 的读不占池（#4620）**：MEMORIES 扩展接管存储的 bank，记忆不在 SQL 里，但七条引擎路径原先还是开一条池化连接"递给"远端读——槽位被占满整次调用。`_store_read_conn`（memory_engine.py:6141-6159）对 store-owned bank yield `None`、其余照旧借池；仅覆盖"连接除了被 store 读忽略外别无用途"的块，store 自己仍要 SQL 的调用（实体名、图视图）保留 `acquire_with_retry`。
- **store-owned bank 不建 per-bank 向量索引（#4659）**：这类 bank 的 memory_units 恒空，三个 partial HNSW 空索引不免费——一个 27315 个 store-owned bank 的租户带着 82795 个空索引，为共享表上每条语句多付约 975ms 规划时间；`create_bank_vector_indexes` 现在跳过 store-owned bank（guard 在被调方，restore/import 路径同享）。

---

# 第 4 层【必答问题速答】

1. **REST 端点地图 / MCP 工具**：见 1.4（99 条路由，10 大组）与 1.3（MCP 39 工具全量 / 单 bank 36）。
2. **retain/recall/reflect 同异步**：recall、reflect 只有同步形态（disconnect 可取消）；retain 是同步/异步二合一（`async` 参数），异步路径 HTTP 200 + body `async:true` + `operation_id`，客户端轮询 operations。幂等键仅 async retain 有（客户端自选 UUID 作 parent operation_id，重放不重复入库，memory_engine.py:22145-22172）；其他异步操作靠提交期 dedupe（bank 级或 payload 键级）。
3. **任务系统**：队列表 `async_operations`（3.1）；认领 = 轮询 schema 扫描 + rot/fifo 单语句 `FOR UPDATE SKIP LOCKED` + `mark_operations_processing`（3.2）；失败恢复 = retry/defer（store 背压与 append 冲突都不计重试）/wall-timeout/终态写库限期重试否则自杀/启动后台恢复 + 关停归还（3.3）；import-bank 的崩溃重试语义见 3.9。worker 与 API 共表共代码，默认同进程、可分离。
4. **扩展槽位**：6 个（TENANT / OPERATION_VALIDATOR / HTTP / MCP / MEMORY_DEFENSE / MEMORIES），env-var 驱动的 import-string 加载协议 + config 前缀收集（3.6）。
5. **生产形态**：单进程可跑（pg0 内嵌）；多副本注意迁移幂等、准入按进程、metrics 多 worker 合并、worker_id 稳定性、按副本放大槽位（3.8）。
6. **admission 拒绝条件**：lane 启用且排队超 `max_wait_seconds`（recall 30s / reflect 5s / retain 2s 默认）→ 503 + Retry-After；排队中客户端断连 → 499；负数 env 关 lane；限额按 worker 进程计（3.4）。
7. **怎么把一个 bank 搬到别处/换 id**：整库走 `POST transfer/export` → `POST transfer/import`（restore）；同实例换 id 走别名零停机迁移（`POST aliases`）；本实例复制走 `POST clone`（3.9）。

# 第 5 层【未能确认 / 存疑】

1. （已结案）`async_operations.status` 的 CHECK 约束扩入 `cancelled`：初始迁移只列四态（5a366d414dce:237-239），`i4j5k6l7m8n9_add_cancelled_status_to_async_operations.py:28-31` 以 DROP 旧约束 + ADD 新约束的方式扩为五值（'pending','processing','completed','failed','cancelled'）。`cancelled` 的写入点：worker 侧父聚合置 cancelled 在 `worker/poller.py:1024`，API 侧 `cancel_operation` 在 `engine/memory_engine.py:21292`（UPDATE 带 `status IN ('pending','processing')` 守卫重查，21349-21350）。
2. `OperationValidatorExtension.validate_consolidate / on_consolidate_complete`：接口已发布、api-slim 全库 grep 无调用点（子代理核实）；推测为闭源云版使用，但无法在 OSS 内证实其存在。
3. `mcp_tools.py` 顶部 docstring 称 `hindsight-local-mcp` 为 stdio transport，与 `mcp_local.py` 自身 docstring（HTTP transport，8888 端口）矛盾——按实现判断 docstring 过时，但不排除存在旧的 stdio 路径历史。
4. `hindsight-all-npm` 的 `server.ts` 流程（profile create → daemon start → /health 轮询）来自子代理阅读；`hindsight-embed` 各端口/命令解析顺序未逐一复跑。
5. `_claim_reserved_tasks`（非 consolidation 的预留池认领 SQL）未逐行核对，仅核实了共享池与 consolidation 两条认领路径及统一的 `claim_tasks` 两阶段结构。
