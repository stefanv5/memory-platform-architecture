# 平台通用参考扩展：代码骨架设计

本文展开 [`huawei-cloud-serverless.md`](huawei-cloud-serverless.md) §6.5 引擎接入合同的 **C2（租户解析）/ C3（配额 precheck）/ C4（计量事件）** 的参考实现：一个可直接开发的 Python 扩展包 `hindsight-ext-platform`。它加载进每个 Hindsight 引擎进程（API 与 worker 同一份），把引擎对平台的依赖收敛为**对 L2 平台服务的 HTTP 调用 + 本地缓存**——不直连平台库，不持有平台库凭据。新引擎（非 Hindsight）替换时，在适配器内实现同一组 HTTP 契约即可。

接口事实均核对引擎源码 `12f2d54`：`TenantExtension`（`core/extensions/tenant.py:54-223`）、`OperationValidatorExtension`（`core/extensions/operation_validator.py:553-1019`）、`Extension` 基类（`core/extensions/base.py:10-81`）、加载器（`core/extensions/loader.py:24-125`）。本文是**设计骨架**（可落地的代码蓝图），不是已测试的实现；标注 🟧 的语义决策需在阶段 B 验收时验证。

## 1. 运行位置与数据流

~~~mermaid
flowchart TB
  subgraph ENG["hindsight 引擎进程（api 或 worker）"]
    TE["PlatformTenantExtension 🟧<br/>authenticate/list_tenants/get_tenant_config"]:::platform
    VE["PlatformValidatorExtension 🟧<br/>precheck/validate_*/on_*_complete"]:::platform
    ME["MeteringEmitter 🟧<br/>有界队列+后台发送"]:::platform
  end
  subgraph L2P["L2 平台服务（双实例）"]
    REG["注册服务 /internal/*<br/>tenants·keys·config"]:::platform
    QUOTA["配额服务 /internal/quota/check"]:::platform
    METER["计量接收 /internal/metering"]:::platform
  end
  REQ["请求（带 X-Platform-Assertion）"]:::external --> TE
  TE -->|"断言验证(HMAC·本地)"| TE
  TE -->|"注册表查询(缓存 TTL)"| REG
  VE -->|"配额判定(fail-close·薄HTTP)"| QUOTA
  ME -->|"usage 事件(event_id 幂等)"| METER
  classDef platform fill:#FFEDD5,stroke:#C2410C,color:#7C2D12;
  classDef external fill:#F8FAFC,stroke:#64748B,color:#0F172A;
~~~

设计不变量：**引擎进程对平台的全部依赖 = 三个 HTTP 端点 + 一个共享密钥**。平台库故障的爆炸半径被 L2 的缓存 TTL 界定（主文档 §9-F6）。

## 2. 包结构

```
hindsight-ext-platform/
├── pyproject.toml              # 独立包，无引擎依赖（只用标准库+aiohttp）
├── Dockerfile                  # overlay 在官方镜像上
├── src/hindsight_ext_platform/
│   ├── __init__.py
│   ├── config.py               # HS_PLATFORM_* env → 冻结配置
│   ├── assertion.py            # 平台断言的签发/验证（HMAC）
│   ├── client.py               # 共享 HTTP 客户端（每进程一个 session）
│   ├── registry.py             # 注册表客户端 + 三级 TTL 缓存
│   ├── tenant.py               # PlatformTenantExtension
│   ├── validator.py            # PlatformValidatorExtension
│   └── metering.py             # UsageEvent + MeteringEmitter
└── tests/
    ├── test_assertion.py       # 纯函数
    ├── test_tenant.py          # mock HTTP（对齐仓库集成测试要求）
    └── test_validator_metering.py
```

## 3. 配置与加载机制

引擎加载器事实（`loader.py:24-125`）：`HINDSIGHT_API_TENANT_EXTENSION=module:Class` 选择实现；**同槽位前缀**的其余 env 变量（如 `HINDSIGHT_API_TENANT_FOO`）被小写化去前缀后塞进 `config: dict[str, str]`。

🟧 **设计决策：包级配置用自有前缀 `HS_PLATFORM_*`，不走 loader 的槽位前缀。** 理由：租户与验证器是两个槽位（前缀分别是 `HINDSIGHT_API_TENANT_*` / `HINDSIGHT_API_OPERATION_VALIDATOR_*`），共享配置若走槽位前缀每个变量要写两遍；自有前缀一次生效于两个扩展，且仍是纯 env、零引擎改动。

```python
# src/hindsight_ext_platform/config.py
"""HS_PLATFORM_* 环境变量 → 冻结配置。进程启动时读一次。"""
from __future__ import annotations
import os
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class PlatformConfig:
    registry_url: str            # L2 注册服务基址（如 http://platform-internal:9101）
    metering_url: str            # L2 计量接收基址
    cell_id: str                 # 本引擎 Cell 标识（决定 list_tenants 的返回范围）
    assertion_secret: str        # 与 L2 共享的 HMAC 密钥（KMS 注入；轮换时新旧双活）
    tenant_cache_ttl: float = 300.0     # 租户→schema 缓存；= key 撤销生效 SLA（主文档 §3.5）
    tenant_list_ttl: float = 60.0       # list_tenants 缓存（worker 每 500ms 调，必须内存命中）
    tenant_config_ttl: float = 60.0     # get_tenant_config 缓存（引擎每请求调，不缓存）
    allow_direct_key: bool = False      # 自托管模式：无 L2 前门时按 key 哈希查注册表
    quota_fail_open: bool = False       # 仅开发环境：配额服务故障时放行（生产必须 False）
    max_sync_retain_bytes: int = 512_000  # 超过即拒绝同步 retain（precheck 强制异步的阈值）
    meter_queue_size: int = 10_000

    @classmethod
    def from_env(cls) -> PlatformConfig:
        def _f(name: str, default: float) -> float:
            return float(os.environ.get(name, default))
        return cls(
            registry_url=os.environ["HS_PLATFORM_REGISTRY_URL"],
            metering_url=os.environ["HS_PLATFORM_METERING_URL"],
            cell_id=os.environ["HS_PLATFORM_CELL_ID"],
            assertion_secret=os.environ["HS_PLATFORM_ASSERTION_SECRET"],
            tenant_cache_ttl=_f("HS_PLATFORM_TENANT_CACHE_TTL", 300.0),
            tenant_list_ttl=_f("HS_PLATFORM_TENANT_LIST_TTL", 60.0),
            tenant_config_ttl=_f("HS_PLATFORM_TENANT_CONFIG_TTL", 60.0),
            allow_direct_key=os.environ.get("HS_PLATFORM_ALLOW_DIRECT_KEY", "").lower() == "true",
            quota_fail_open=os.environ.get("HS_PLATFORM_QUOTA_FAIL_OPEN", "").lower() == "true",
            max_sync_retain_bytes=int(os.environ.get("HS_PLATFORM_MAX_SYNC_RETAIN_BYTES", 512_000)),
            meter_queue_size=int(os.environ.get("HS_PLATFORM_METER_QUEUE_SIZE", 10_000)),
        )
```

## 4. 平台断言（认证的信任根）

断言是 L2 鉴权服务在转发请求时注入的短期凭证，证明"平台已验证过这个租户"。它是纵深防御的第二层（第一层是网络隔离：引擎端口只收 ELB/L2 流量，主文档 §4.3）。

```python
# src/hindsight_ext_platform/assertion.py
"""X-Platform-Assertion: v1.<b64url(payload)>.<b64url(hmac)>

payload = {"iss":"hs-platform","tenant_id":...,"key_id":...,
           "banks":[...]|None,"iat":unix,"exp":unix}   # TTL ≤ 60s
HMAC-SHA256(secret, "v1." + b64url(payload))，与 L2 鉴权服务共享密钥。
密钥轮换：L2 与扩展同时持有新旧两把（config 注入为逗号分隔），验证按序尝试。
"""
from __future__ import annotations
import base64, hashlib, hmac, json, time
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class AssertionClaims:
    tenant_id: str
    key_id: str
    banks: tuple[str, ...] | None   # None = 不限制（平台已在 L2 做过 bank ACL）

class AssertionError(ValueError): ...

def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def verify_assertion(header: str, secrets: tuple[str, ...], *,
                     now: float | None = None, max_skew_s: float = 5.0) -> AssertionClaims:
    now = time.time() if now is None else now
    try:
        version, payload_b64, mac_b64 = header.split(".")
        if version != "v1":
            raise AssertionError("unsupported assertion version")
        payload = json.loads(_b64url_decode(payload_b64))
        if payload.get("iss") != "hs-platform":
            raise AssertionError("wrong issuer")
        if not (payload["iat"] - max_skew_s <= now <= payload["exp"] + max_skew_s):
            raise AssertionError("assertion expired")
        signed = ("v1." + payload_b64).encode()
        if not any(hmac.compare_digest(
                hmac.new(s.encode(), signed, hashlib.sha256).digest(),
                _b64url_decode(mac_b64)) for s in secrets):
            raise AssertionError("bad signature")
        return AssertionClaims(
            tenant_id=payload["tenant_id"], key_id=payload["key_id"],
            banks=tuple(payload["banks"]) if payload.get("banks") is not None else None)
    except AssertionError:
        raise
    except Exception as e:  # noqa: BLE001 — 对外统一拒绝
        raise AssertionError(f"malformed assertion: {e}") from e
```

L2 侧签发函数（同文件，供平台服务复用）：`sign_assertion(claims, secret, ttl_s=60) -> str`。

## 5. 共享 HTTP 客户端与注册表缓存

```python
# src/hindsight_ext_platform/client.py
"""进程级共享 aiohttp 客户端。on_startup 创建、on_shutdown 关闭。
超时收紧（connect 1s / total 3s）：这些调用在请求路径上，宁可快速失败。"""
from __future__ import annotations
import aiohttp

class PlatformUnavailable(RuntimeError): ...

class PlatformClient:
    def __init__(self, base_url: str, internal_token: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = internal_token
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=aiohttp.ClientTimeout(connect=1.0, total=3.0))

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def get_json(self, path: str) -> dict | list:
        assert self._session is not None, "start() not called"
        try:
            async with self._session.get(self._base + path) as r:
                if r.status == 404:
                    raise KeyError(path)
                r.raise_for_status()
                return await r.json()
        except aiohttp.ClientError as e:
            raise PlatformUnavailable(str(e)) from e

    async def post_json(self, path: str, body: dict) -> dict:
        assert self._session is not None
        try:
            async with self._session.post(self._base + path, json=body) as r:
                r.raise_for_status()
                return await r.json()
        except aiohttp.ClientError as e:
            raise PlatformUnavailable(str(e)) from e
```

```python
# src/hindsight_ext_platform/registry.py
"""注册表访问 + 三级 TTL 缓存。全部可重建（主文档 §3.5 无状态定义）。"""
from __future__ import annotations
import asyncio, time
from hindsight_ext_platform.client import PlatformClient, PlatformUnavailable

class RegistryCache:
    def __init__(self, client: PlatformClient, cell_id: str,
                 tenant_ttl: float, list_ttl: float, config_ttl: float) -> None:
        self._c, self._cell = client, cell_id
        self._ttls = (tenant_ttl, list_ttl, config_ttl)
        self._tenant: dict[str, tuple[float, dict]] = {}    # tenant_id → (expires, entry)
        self._by_hash: dict[str, tuple[float, dict]] = {}   # key 哈希 → (expires, entry) [direct-key 模式]
        self._tconf: dict[str, tuple[float, dict]] = {}     # tenant_id → (expires, config dict)
        self._tenant_list: tuple[float, list] = (0.0, [])   # 单例缓存（worker 每 500ms 读）
        self._lock = asyncio.Lock()                          # single-flight：防缓存过期瞬间的惊群

    async def get_tenant(self, tenant_id: str) -> dict:
        """→ {"schema_name", "status", "allowed_bank_ids", ...}；缓存过期时单飞刷新。"""
        now = time.monotonic()
        hit = self._tenant.get(tenant_id)
        if hit and hit[0] > now:
            return hit[1]
        async with self._lock:
            hit = self._tenant.get(tenant_id)          # double-check
            if hit and hit[0] > time.monotonic():
                return hit[1]
            entry = await self._c.get_json(f"/internal/tenants/{tenant_id}")
            self._tenant[tenant_id] = (time.monotonic() + self._ttls[0], entry)
            return entry

    async def list_tenants(self) -> list[dict]:
        """本 Cell 的 [{schema, tenant_id, status}]；worker 轮询路径，必须内存命中。"""
        if self._tenant_list[0] > time.monotonic():
            return self._tenant_list[1]
        async with self._lock:
            if self._tenant_list[0] > time.monotonic():
                return self._tenant_list[1]
            items = await self._c.get_json(f"/internal/cells/{self._cell}/tenants")
            self._tenant_list = (time.monotonic() + self._ttls[1], list(items))
            return self._tenant_list[1]

    async def get_tenant_config(self, tenant_id: str) -> dict:
        """层级配置的租户级覆盖（引擎每请求调用）。失败时 fail-open 返回 {}：
        行为覆盖缺失只退回全局默认，不构成安全问题（主文档 §6.5-C2）。"""
        try:
            if (h := self._tconf.get(tenant_id)) and h[0] > time.monotonic():
                return h[1]
            cfg = await self._c.get_json(f"/internal/tenants/{tenant_id}/config")
            self._tconf[tenant_id] = (time.monotonic() + self._ttls[2], cfg)
            return cfg
        except PlatformUnavailable:
            return {}
```

## 6. PlatformTenantExtension（接入合同 C2）

```python
# src/hindsight_ext_platform/tenant.py
from hindsight_api.extensions.tenant import (
    AuthenticationError, Tenant, TenantContext, TenantExtension)
from hindsight_api.models import RequestContext

from .assertion import verify_assertion, AssertionClaims, AssertionError
from .client import PlatformClient, PlatformUnavailable
from .config import PlatformConfig
from .registry import RegistryCache

_ASSERTION_HEADER = "x-platform-assertion"   # 须列入 HINDSIGHT_API_EXTENSION_PASSTHROUGH_HEADERS

class PlatformTenantExtension(TenantExtension):
    """每请求被引擎调用（引擎不缓存——缓存是本扩展的职责，主文档 §3.5）。"""

    def __init__(self, config: dict[str, str]) -> None:   # config 来自 loader，未用（见 §3 决策）
        super().__init__(config)
        self._cfg = PlatformConfig.from_env()
        self._client = PlatformClient(self._cfg.registry_url, internal_token="engine")
        self._registry = RegistryCache(self._client, self._cfg.cell_id,
                                       self._cfg.tenant_cache_ttl,
                                       self._cfg.tenant_list_ttl,
                                       self._cfg.tenant_config_ttl)

    async def on_startup(self) -> None:
        await self._client.start()

    async def on_shutdown(self) -> None:
        await self._client.close()

    # ---- 主路径：断言（L2 前门模式）----
    async def authenticate(self, context: RequestContext) -> TenantContext:
        claims = self._claims(context)
        context.tenant_id = claims.tenant_id          # 供计量归属（引擎 RequestContext 语义）
        context.api_key_id = claims.key_id
        try:
            entry = await self._registry.get_tenant(claims.tenant_id)
        except PlatformUnavailable as e:
            # fail-close：解析不出 schema 就拒绝（可用性损失已在 F6 声明，缓存 TTL 内不受影响）
            raise AuthenticationError("tenant registry unavailable") from e
        if entry.get("status") != "active":
            raise AuthenticationError("tenant is not active")   # frozen/deleting
        # bank ACL 写回 RequestContext（引擎核心不消费，本包 validator 消费——主文档 §1.4）
        if claims.banks is not None:
            context.allowed_bank_ids = list(claims.banks)
        return TenantContext(schema_name=entry["schema_name"])

    # ---- 可选：无前门直连模式（自托管）----
    def _claims_direct(self, context: RequestContext) -> AssertionClaims:
        """allow_direct_key=true 时：按 key 哈希查注册表（L2 提供 /internal/keys/by-hash）。
        适合单租户/私有化部署；多租户 SaaS 必须走断言模式。"""
        ...  # sha256(context.api_key) → registry._by_hash → claims

    def _claims(self, context: RequestContext) -> AssertionClaims:
        header = (context.extra_headers or {}).get(_ASSERTION_HEADER)
        if header is not None:
            return verify_assertion(header,
                                    tuple(self._cfg.assertion_secret.split(",")))
        if self._cfg.allow_direct_key:
            return self._claims_direct(context)
        raise AuthenticationError("missing platform assertion")

    # ---- worker/迁移侧：schema 发现 ----
    async def list_tenants(self) -> list[Tenant]:
        """引擎 poller 每轮调用（500ms）——实现为内存缓存 + 60s 刷新（registry.list_tenants）。
        返回的 schema 集合决定 worker 扫描范围；frozen 租户被平台侧过滤，天然停止领取。"""
        items = await self._registry.list_tenants()   # PlatformUnavailable 由引擎按轮次跳过
        return [Tenant(schema=i["schema"], tenant_id=i.get("tenant_id"))
                for i in items if i.get("status") == "active"]

    # ---- 层级配置的租户级 ----
    async def get_tenant_config(self, context: RequestContext) -> dict[str, object]:
        return await self._registry.get_tenant_config(context.tenant_id or "")

    # ---- MCP 传输 ----
    # 不覆写 authenticate_mcp：基类默认转发到 authenticate()。断言 header 经
    # HINDSIGHT_API_EXTENSION_PASSTHROUGH_HEADERS 放行后对 MCP 同样可达（源码已证，
    # core/api/mcp.py:456,477）。前提：MCP_AUTH_TOKEN 严禁设置（主文档 §4.3）。
```

## 7. PlatformValidatorExtension（接入合同 C3 + C4）

```python
# src/hindsight_ext_platform/validator.py
from hindsight_api.extensions.operation_validator import (
    OperationValidatorExtension, PrecheckContext, ValidationResult,
    RetainResult, RecallResult, ReflectResultContext, ConsolidateResult)
from hindsight_api.models import RequestContext

from .client import PlatformClient, PlatformUnavailable
from .config import PlatformConfig
from .metering import MeteringEmitter, UsageEvent

class PlatformValidatorExtension(OperationValidatorExtension):
    def __init__(self, config: dict[str, str]) -> None:
        super().__init__(config)
        self._cfg = PlatformConfig.from_env()
        self._quota = PlatformClient(self._cfg.registry_url, internal_token="engine")
        self._meter = MeteringEmitter(self._cfg.metering_url, self._cfg)

    async def on_startup(self) -> None:
        await self._quota.start(); await self._meter.start()

    async def on_shutdown(self) -> None:
        await self._meter.flush_and_stop(timeout=5.0); await self._quota.close()

    # ============ precheck（body 解析前；主文档 §4.4 fail-close）============
    async def precheck(self, ctx: PrecheckContext) -> ValidationResult:
        # 注意：引擎默认建议"出错时放行"（operation_validator.py:618-621 docstring）。
        # 本扩展对配额服务故障**故意偏离为 fail-close**：配额是业务边界，F6 已声明
        # 该语义；开发环境可用 quota_fail_open 回到引擎默认。
        if ctx.request_context.tenant_id is None:
            return ValidationResult.reject("unauthenticated", 401)
        if ctx.request_context.allowed_bank_ids is not None and \
                ctx.bank_id not in ctx.request_context.allowed_bank_ids:
            return ValidationResult.reject(f"bank '{ctx.bank_id}' not permitted", 403)
        if ctx.operation.value == "retain" and ctx.content_length is not None \
                and ctx.content_length > self._cfg.max_sync_retain_bytes:
            # 大内容拒绝同步路径，客户端以 async=true 重试（precheck 无法改写 body，
            # 强制异步由拒绝+明确 reason 达成——客户端契约）
            return ValidationResult.reject(
                f"content exceeds sync limit ({self._cfg.max_sync_retain_bytes}B); "
                "retry with async=true", 413)
        try:
            verdict = await self._quota.post_json("/internal/quota/check", {
                "tenant_id": ctx.request_context.tenant_id,
                "operation": ctx.operation.value,
                "bank_id": ctx.bank_id,
                "content_length": ctx.content_length,   # chunked 时为 None → 平台侧
                                                             # 按配置拒绝(411)或按最大值保守判定
            })
        except PlatformUnavailable:
            if self._cfg.quota_fail_open:
                return ValidationResult.accept()
            return ValidationResult.reject("quota service unavailable", 503)
        if not verdict.get("allowed", True):
            return ValidationResult.reject(verdict.get("reason", "quota exceeded"),
                                           verdict.get("status_code", 429))
        return ValidationResult.accept()

    # ============ 后 body 校验（抽象方法，必须实现）============
    async def validate_retain(self, ctx) -> ValidationResult:      # RetainContext
        return self._acl(ctx.bank_id, ctx.request_context)
    async def validate_recall(self, ctx) -> ValidationResult:      # RecallContext
        return self._acl(ctx.bank_id, ctx.request_context)
    async def validate_reflect(self, ctx) -> ValidationResult:     # ReflectContext
        return self._acl(ctx.bank_id, ctx.request_context)

    def _acl(self, bank_id: str, rc: RequestContext) -> ValidationResult:
        if rc.allowed_bank_ids is not None and bank_id not in rc.allowed_bank_ids:
            return ValidationResult.reject(f"bank '{bank_id}' not permitted", 403)
        return ValidationResult.accept()

    # ============ bank 管理面（ACL + 镜像同步）============
    async def validate_bank_read(self, ctx) -> ValidationResult:
        return self._acl(ctx.bank_id, ctx.request_context)

    async def validate_bank_write(self, ctx) -> ValidationResult:
        # bank_registry 镜像回填：删除/更新路径上 fire-and-forget 通知平台
        # （准实时；每日对账兜底——主文档 §9 数据层保障-3）
        return self._acl(ctx.bank_id, ctx.request_context)

    async def filter_bank_list(self, ctx) -> "BankListResult":    # 引擎类型
        # 过滤 allowed_bank_ids 之外的 bank，并镜像回填列表（对账源）
        ...

    # ============ 计量（接入合同 C4；hook 带 token 字段，源码 236-291）============
    async def on_retain_complete(self, result: RetainResult) -> None:
        self._meter.emit(UsageEvent.from_retain(result))
    async def on_recall_complete(self, result: RecallResult) -> None:
        self._meter.emit(UsageEvent.from_recall(result))
    async def on_reflect_complete(self, result: ReflectResultContext) -> None:
        self._meter.emit(UsageEvent.from_reflect(result))
    async def on_consolidate_complete(self, result: ConsolidateResult) -> None:
        self._meter.emit(UsageEvent.from_consolidation(result))
```

## 8. MeteringEmitter（计量事件的可靠发送）

```python
# src/hindsight_ext_platform/metering.py
"""非阻塞 emit + 后台批量发送 + 重试。语义：at-least-once（传输级用 event_id 幂等，
平台侧 UNIQUE(event_id) 去重）；进程死亡丢失队列内未发事件（与引擎 audit_log 同级
best-effort——精确计量以平台消费引擎 webhook 的操作级事件对账，见 §10 限制）。"""
from __future__ import annotations
import asyncio, dataclasses, logging, time, uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .client import PlatformClient

if TYPE_CHECKING:
    from hindsight_api.extensions.operation_validator import RetainResult

log = logging.getLogger("hindsight_ext_platform.metering")

@dataclass(slots=True)
class UsageEvent:
    event_id: str; kind: str                       # retain|recall|reflect|consolidation
    tenant_id: str; bank_id: str
    success: bool
    llm_input_tokens: int = 0; llm_output_tokens: int = 0; llm_total_tokens: int = 0
    llm_cached_input_tokens: int = 0; llm_thoughts_tokens: int = 0
    processed_content_tokens: int = 0
    document_id: str | None = None
    folded_with: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    # None→0 是引擎规定的计量语义（RetainResult docstring："treat None as 0"）。
    # fold 语义：一次折叠执行对每个成员各触发一次 hook，用量记在首个成员、其余为 0
    # ——全部上报，平台按 fold 求和即得该次执行的真实用量（源码注释 285-291）。
    @classmethod
    def from_retain(cls, r: RetainResult) -> "UsageEvent": ...   # 字段映射，None→0

class MeteringEmitter:
    def __init__(self, base_url: str, cfg) -> None:
        self._client = PlatformClient(base_url, internal_token="engine")
        self._max = cfg.meter_queue_size
        self._queue: asyncio.Queue[UsageEvent] = asyncio.Queue(maxsize=self._max)
        self._task: asyncio.Task | None = None
        self._dropped = 0

    async def start(self) -> None:
        await self._client.start()
        self._task = asyncio.create_task(self._run(), name="platform-metering")

    def emit(self, event: UsageEvent) -> None:
        try:
            self._queue.put_nowait(event)          # 绝不阻塞业务路径
        except asyncio.QueueFull:
            self._dropped += 1                     # 丢最旧语义由有界队列近似；
            if self._dropped % 1000 == 1:          # 计数暴露给采集器巡检（告警项）
                log.warning("metering queue full, dropped≈%d", self._dropped)

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            batch: list[UsageEvent] = []
            ev = await self._queue.get()           # 阻塞等第一个
            batch.append(ev)
            while len(batch) < 100 and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._client.post_json("/internal/metering",
                    {"events": [dataclasses.asdict(e) for e in batch]})
                backoff = 1.0
            except Exception:                      # noqa: BLE001 — 后台任务不允许死
                await asyncio.sleep(backoff); backoff = min(backoff * 2, 30.0)
                for e in batch:                    # 重放（event_id 不变 → 平台幂等去重）
                    self.emit(e)

    async def flush_and_stop(self, timeout: float = 5.0) -> None:
        if self._task is not None:
            self._task.cancel()
            try: await asyncio.wait_for(self._task, timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError): pass
        await self._client.close()
```

## 9. 部署

```dockerfile
# Dockerfile —— overlay 在官方镜像上（hindsight-extensions 同款模式）
FROM ghcr.io/vectorize-io/hindsight-api:<version>
COPY src/hindsight_ext_platform/ /app/extensions/hindsight_ext_platform/
ENV PYTHONPATH=/app/extensions
```

引擎侧环境变量（api 组与 worker 组**完全相同**——worker 加载同一 TENANT 扩展做 schema 发现，`core/worker/main.py:271-289`）：

```bash
# 槽位选择
HINDSIGHT_API_TENANT_EXTENSION=hindsight_ext_platform.tenant:PlatformTenantExtension
HINDSIGHT_API_OPERATION_VALIDATOR_EXTENSION=hindsight_ext_platform.validator:PlatformValidatorExtension
# 断言 header 放行（不放行则 header 到不了 authenticate —— models.py:44-51）
HINDSIGHT_API_EXTENSION_PASSTHROUGH_HEADERS=x-platform-assertion
# 平台配置（HS_PLATFORM_* 见 §3；assertion_secret 由 KMS 注入）
HS_PLATFORM_REGISTRY_URL=http://platform-internal.cell-a.svc:9101
HS_PLATFORM_METERING_URL=http://platform-internal.cell-a.svc:9102
HS_PLATFORM_CELL_ID=cell-a
HS_PLATFORM_ASSERTION_SECRET=<kms:current>,<kms:previous>   # 轮换双活
# 禁项（多租户硬约束）
# HINDSIGHT_API_MCP_AUTH_TOKEN  —— 严禁设置（MCP 旁路，主文档 §4.3）
```

## 10. 已知限制与上游演进项

| # | 限制 | 影响 | 缓解 / 上游演进 |
|---|---|---|---|
| L1 | `RetainResult` 不携带 operation_id（只有 `folded_with` 成员的 id） | 计量幂等只能用 event_id（传输级）；引擎崩溃重执行会产生第二份计量 | 平台按 fold 求和对冲多数场景；上游增强：完成钩子上下文带 operation_id |
| L2 | 引擎 webhook（操作完成事件）payload 不含 token 用量 | 计量无法完全走事务性 outbox（会丢进程死亡窗口内的事件） | 本方案 on_complete+HTTP；上游增强后可切 webhook 消费（零丢失） |
| L3 | precheck 无法改写 body | "大内容强制异步"只能以 413+reason 拒绝实现（客户端契约），不是服务端透明转换 | 产品模式（L2 产品 API）可透明改写；直通模式接受此契约 |
| L4 | `list_tenants` 缓存 60s | 新租户开通后最多 60s 才被 worker 发现 | 开通控制器完成后可向 worker 组发 SIGHUP/重启滚动；或接受 60s |
| L5 | 两扩展各自实例化（loader 语义） | 每进程两个扩展对象；HTTP 客户端按"每扩展一个 session"计 2 个连接 | 连接预算已计入（主文档 §8.1-④）；可改为模块级共享单例（骨架保留简单形态） |
| L6 | `quota_fail_open` 仅供开发 | 生产 fail-close 意味着配额服务故障 = 数据面拒绝（F6 已声明） | L2 配额服务双实例 + 缓存；接受可用性换边界 |

## 11. 测试要点（对齐仓库集成测试标准：必须模拟外部系统）

1. **断言**：过期/错密钥/篡改 payload/双密钥轮换窗口——纯函数直测。
2. **租户扩展**：mock 注册服务（aiohttp test server）——缓存命中不发请求；`PlatformUnavailable` → `AuthenticationError`；frozen 租户拒绝；`list_tenants` 在 500ms 连续调用下只发一次 HTTP（single-flight）。
3. **验证器**：chunked（content_length=None）走平台策略；411/413/429/503 各分支；bank ACL 拒绝。
4. **计量**：队列满丢计数；批量+重试幂等（同 event_id 重发，平台 mock 按 UNIQUE 去重）；fold 多成员事件求和正确。
5. **端到端**（阶段 B 验收）：双租户并发走真实引擎进程 + mock L2，断言零串扰（主文档 §10-B）。
