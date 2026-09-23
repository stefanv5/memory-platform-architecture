# Serverless与共享Worker：Cognee执行生命周期的8项结论

基线：Cognee `663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e`。本节复用V5执行证据，仅补充读取后台任务注册与执行模式；不扩展认证/存储审计。以下严格区分当前事实与需要实现的弹性执行设计，没有进行缩容、冷启动或故障实验。

## 1. 不要求一客户一套Cognee进程；隔离数据与独占计算是两件事

**事实：** dataset-aware runner按授权Dataset推进，每个Dataset传入自己的user、数据与上下文；没有在这一层要求“一个租户启动一个进程”。[pipeline.py L94–148](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/pipeline.py#L94-L148)

**建议：** 多个客户的space可共享同一Cell及Worker池，一个Worker先后处理不同space。平台固定每次Job的tenant/space/binding和模型配置，完整恢复上下文，并用租户配额防止占满资源。客户只有购买专属计算/数据面等级时才需要独立Cell；space也不必一对一映射Pod。此处是运行模式可行性，不等于现有全部配置和会话路径已经完成隔离改造。

## 2. 共享弹性Worker临时持有space写入权，不等于每个space常驻一个Pod

**事实：** 当前dataset锁只在进程内有效，item并发由每次run自己的semaphore控制；增加进程不会共享这些控制状态。[dataset_lock.py L18–34](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/locks/dataset_lock.py#L18-L34)、[run_tasks.py L119–128](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/run_tasks.py#L119-L128)

**建议：** 对已通过并发验收的远程数据库路线，Worker从持久队列领取Job，只在该space的修改期间取得写入所有权；完成或安全退出后释放，再处理别的space。不同space可在多个Worker并行，同space的更新/删除/improve统一串行或遵守显式版本协议。一个Worker可以并发少量不同space，但上限受内存、连接和模型预算约束。缩到N个Worker只改变同时处理能力，目录里的M个space不要求保留M个空闲Pod。

临时所有权仍须防止旧Worker迟到写入：下游fencing、隔离attempt产物与发布，或确认旧环境已停止。仅外层租约超时不赋予安全重试权。

## 3. 嵌入式图要有明确owner，但owner不是永久绑定客户的专属机器

**事实：** DatasetQueue在最后持有者释放后可触发子进程engine保温或关闭，默认空闲TTL为600秒；这是进程内缓存/文件生命周期协调。[queue.py L78–93](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/dataset_queue/queue.py#L78-L93)、[L265–287](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/dataset_queue/queue.py#L265-L287)、[L450–468](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/dataset_queue/queue.py#L450-L468)

**建议：** 嵌入式图运行时的所有读写由唯一受控owner执行；该owner可承载多个客户的多个space，不是每space一个Pod。空闲时可排空请求、flush/close文件并记录安全停止状态后休眠，下一次由控制器启动/绑定owner。迁移owner也可做，但必须确认旧实例及数据库子进程停止或存储访问已被可靠隔离；不能把PVC同时挂到任意临时Worker直接打开。

所以应区分：**远程数据库下的共享临时写Worker**与**嵌入式数据库打开期间的固定访问owner**。后者也能按需启停，但冷启动、卷挂载、模型初始化及恢复检查都有成本，不是透明的随机副本路由。

## 4. 当前“后台运行”不是支持缩零的持久执行

**事实：** run_pipeline_as_background_process通过本事件循环create_task推进pipeline，其强引用只避免被GC；进程退出仍会丢失内存执行。[pipeline_execution_mode.py L104–127](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/layers/pipeline_execution_mode.py#L104-L127)。run_info中的非Data输入只是512字符摘要，不能用作完整任务重放。[summary.py L5–28](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/utils/summarize_run_info_data.py#L5-L28)

**建议：** 先有原件持久引用、完整Job manifest、固定请求身份、Outbox或直接SQL领取，再让计算实例弹性启停。缩到零之后仍需存在外部可唤醒的任务源、目录和数据；“计算缩零”不等于持久数据和全部费用归零。作业Worker调用Cognee应等待处理真正结束，不把提交内存后台任务当完成。API实例可独立弹性，不能让HTTP响应生命周期承载长任务。

## 5. 重试必须区分业务失败、未知结果与可重做产物

**事实：** 当前增量机制按pipeline_name+dataset跳过完成item，不保存任意内部阶段checkpoint。[item完成判定](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/run_tasks_data_item.py#L197-L246)。runner可yield PipelineRunErrored，并在PipelineRunFailedError路径不再抛异常；因此Worker不能仅以“await没有抛异常”认定成功。[run_tasks.py L323–350](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/operations/run_tasks.py#L323-L350)

**建议：** Consumer明确解析终态并校验副作用；Job/attempt/space epoch各有独立身份。响应丢失先对账，旧attempt静止或隔离后才能补偿重做；补偿失败进入Repair而非无限直接重跑LLM。外部工作流只能在包裹的Activity边界恢复，不会自动保存Cognee内部chunk进度。同space串行减少冲突，却不能代替请求幂等和跨存储失败收敛。

## 6. 缩容drain存在可复用基础，但当前没有覆盖全部后台路径

**事实：** 全局background_tasks registry支持等待当前事件循环里的已注册任务，超时返回False、不取消任务。[background_tasks.py L30–58](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/background_tasks.py#L30-L58)、[L66–99](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/background_tasks.py#L66-L99)。低层pipeline只放入另一个私有集合，没有在该函数调用全局register。[pipeline_execution_mode.py L125–127](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/pipelines/layers/pipeline_execution_mode.py#L125-L127)。已核查API关闭流程默认drain为8秒，随后关engine；不能据此保证长pipeline一定完成。[client.py L82–88](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/client.py#L82-L88)、[L149–173](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/client.py#L149-L173)

**建议：** 缩容流程是停止领取Job和读准入→维持在途所有权→等待任务/读者，或显式停止并await全部子任务→记录可恢复结果→关闭adapter→退出。撤readiness不会自动停止队列消费。超过终止预算时，平台必须按失败恢复路径处理，不能“先释放space租约，后台继续写，稍后再杀Pod”。长任务可选完成后退出或在明确产物边界暂停，两者需不同成本/SLA；不能仅配置一个termination timeout就认定可靠。

## 7. 原生启动恢复不能原样放进频繁冷启动实例

**事实：** API启动调用恢复器；恢复器默认把超过3600秒的STARTED作为候选，并可能补偿cognify，不检查当前任务owner的存活或跨进程lease。[启动调用](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/api/client.py#L129-L131)、[recovery.py L24–45](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/cognify/recovery.py#L24-L45)、[L77–111](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/modules/cognify/recovery.py#L77-L111)

**推断：** 冷启动新API副本可能把其他Worker上仍正常运行的一小时长任务误当遗留作业；频繁启动会增加触发机会。单纯增加阈值只改变窗口，不修复所有权缺失。

**建议：** 把恢复从普通冷启动副作用迁为唯一协调的Recovery/Repair流程；只处理已经证明失效、且取得修复所有权的attempt。Worker尽量有独立启动入口，不为执行库函数启动整套API lifespan。恢复、迁移与资源开通也应受控，不能每次按需唤醒都全库扫描或竞争清理。

## 8. 集群与serverless可行的依据是职责外置，不是伸缩器名称

可以复用现有Task链、dataset上下文、adapter与来源补偿；把待执行工作、space路由、原件及结果状态外置后，计算实例才有条件被替换。DatasetQueue仍作为每实例的本地保护，其slot按(task,dataset)计，不是全局space排他锁。[queue.py L224–263](https://github.com/topoteretes/cognee/blob/663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e/cognee/infrastructure/databases/dataset_queue/queue.py#L224-L263)

建议先完成“共享常驻Worker池→有安全drain的增减副本→空闲缩零与冷启动”三个验证级别。伸缩器（包括KEDA一类实现）负责改变副本数，不能替平台补齐持久任务、幂等、查询屏障、fencing或嵌入式文件接管。

上线前至少实测：队列有作业时从零唤醒；处理中缩容；提交成功但应答丢失；图写成但向量失败；旧owner失联后恢复；新实例启动时另一长任务仍活跃。验收应同时看到无丢Job、无双写文件、无半成品查询、删除不复活，并记录冷启动延迟、重复模型成本与其他租户等待时间。若这些不成立，只能叫自动扩容演示，不能承诺可靠serverless记忆服务。

---

### 本轮阅读记录

本轮全文复读`cognee/infrastructure/background_tasks.py` 1–99与`cognee/modules/pipelines/layers/pipeline_execution_mode.py` 1–142，共241行；没有声称覆盖整个执行模块。其他精确行号复用本线程此前已读的同SHA源码，V5范围见[12-execution-lifecycle.md](12-execution-lifecycle.md)与先前[执行模块分析](06-module-execution.md)。API lifespan链接复用此前生命周期核查，本轮未扩展读取API/auth/storage代码。仅新增本文，不改业务代码，不commit/push。
