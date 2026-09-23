# V5 产品化补查计划与理论依据

目标：将 V4 的可替换引擎边界落到对外产品、权限模型、作业流、实际存储和运营流程。不是实现生产系统；源码现状、目标设计和验收门槛必须分开。

## 叙事路径

企业开户与用户组 → 空间及资料权限 → 一次上传和一次查询 → Cell 路由与物理资源 → 删除、恢复与迁移 → 代码改造工作包。

“多组”同时覆盖企业内用户组和多个计算/存储资源组；二者不得混淆。Tenant 是客户隔离，Group 是授权主体，Space 是共同可见的记忆边界，Cell 是放置与故障边界。

## 分工

- 产品与授权：API、身份、Principal/ACL、SDK/MCP 与可信上下文。
- 存储：Dataset handler、路由、schema/角色、原件、会话与缓存。
- 执行：持久作业、并发、发布、重试、故障及删除生命周期。
- 汇总：外部产品、最小部署选择、跨模块端到端合同与验收。

## 理论依据及适用限制

1. [AWS 租户隔离](https://docs.aws.amazon.com/whitepapers/latest/saas-architecture-fundamentals/tenant-isolation.html)：认证或功能授权本身不能替代租户资源边界；本报告需要同时展示身份、路由和数据访问约束。
2. [PostgreSQL RLS](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)：行级策略可补充共享表隔离；表 owner、superuser、BYPASSRLS 例外必须明确。拟用于新增平台表，不宣称 Cognee 原生已启用。
3. [Kubernetes 多租户](https://kubernetes.io/docs/concepts/security/multi-tenancy/)：namespace 需结合网络、配额和访问控制；命名空间不直接隔离应用数据库，也不等于独立集群。
4. [Transactional Outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)：解决本地状态提交与消息通知双写缺口；重复投递仍需幂等，不能推导图/向量/对象跨库原子事务。

## 验收范围

要求文档具有可定位的源码依据、具体资源示例、各隔离层的实施位置/理由/代价、故障处理与可测试退出标准。定向补查不虚报全仓覆盖。工程实现、容量与 SLA 均仍需运行验证。
