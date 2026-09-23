# 目标方案设计约束（待与完整模块结果交叉核验）

目标不等于全面微服务拆分：保持cognee业务任务库，增加平台接入/耐久作业协议/worker，数据库与对象存储成为网络服务；搜索服务和处理worker按负载分别扩容。

事实源分类：raw对象+文档版本/ACL/作业manifest在SQL是重建输入；图/向量是派生索引，但用户手工图修订、session历史、反馈/学习权重若没有事件记录也可能是不可重建的业务状态，不能简单当cache全部丢弃。

作业协议需 tenant_id / actor_id / owner_id / dataset_id / document_version / content_hash / pipeline_version / model+embedding+prompt版本 / config快照引用 / idempotency_key / input_object_uri / trace_id；不传Python对象、绝对路径、临时文件句柄、密钥明文。tenant来自认证与成员资格验证，不信任客户端自由声明。

SQL队列最低方案：job表为唯一权威记录，短事务SKIP LOCKED领取并落owner/lease/attempt后提交，处理期间不持有SQL行事务；独立reconciler续约失效处理和repair；通知只是唤醒。若使用broker，job和outbox同SQL事务，relay允许重复，worker去重；若Temporal，SQL与workflow启动同样存在双写，仍需outbox+稳定workflow id+对账，不能宣称换引擎自动原子化。

lease只用于发现和接管，不能阻止旧worker继续写。真实安全必须下游事务内校验fencing或不同attempt完全隔离。可先实现dataset generation路由：每次build有独立图/向量namespace和artifact manifest；查询绑定已发布generation；旧worker只能污染未发布的独立build，不能写已发布共享ID。最终通过SQL CAS发布pointer并比对lease/fence。只加ready字段或检索结果末尾过滤不足以阻断图遍历接触半成品。现有adapter须改造，不是配置即可获得。

按dataset整体generation容易证明正确，但重建/复制和双份存储成本较高；细化document级generation需解决跨document共享实体与边的引用和快照读取，不应在首版忽略此成本。短期允许明确的部分可见性也必须产品契约化，不能声称强一致。

删除为tombstone优先（读访问立即拒绝）+后台graph/vector/raw/session范围清理+manifest验证+重试，不先销毁唯一修复依据。所有删除与导入/重建共享dataset串行或版本准入；原件多dataset引用用引用账本；租户注销不能调用全局cache prune。

去重三层分别处理：客户请求幂等、不可变文档版本/内容复用、stage输出与外部副作用幂等。内容一样不等于同一客户意图；工作流重试成功也不保证LLM provider只计费一次。

部署代价：多schema不等于多独立实例，每dataset engine/pool会放大连接预算；graph demo共享写锁会限制吞吐；控制面只增CPU/Pod不能改变这些存储瓶颈。专属实例与池化SaaS用同一job协议、不同资源路由。
