# 交叉验证记录

基线仍为663a2dc15d04bc0d7ec2733a2dd604b7ed1b8c8e，git diff/status无仓库代码变更。

## 回查源码的结论

- 输入字段与身份：ingest_data.py:484-503确认original_data_location为原件、raw_data_location为抽取文本；Data.py:12-20确认普通索引及uuid4，非内容唯一键。
- 存储补偿：add_data_points.py:154-205确认非原生provenance先落SQL ledger；get_unified_engine.py:9-13确认内置hybrid注册表为空。报告没有将PG图/向量同库等同同事务。
- 普通用户全局cache删除：forget.py:191-218、get_forget_router.py、SqlCacheAdapter.py:1196-1211、RedisAdapter.py:681-686交叉确认。SQL docstring写四表但实际for循环为五表，报告以实现为准。未做破坏性运行实验。
- 调度：dataset_lock.py:18-20明确process-local；pipeline_execution_mode末段创建asyncio.Task，无远程worker。
- 恢复：recovery.py全文确认默认3600秒按created_at判断，没有owner/heartbeat claim；run_tasks.py:210-215及292-340确认gather与rollback错误处理。竞态标为推断待故障注入，未声称已复现。
- 多租户：select_tenant.py全文确认用户活动tenant写User行；SqlCacheAdapter.py:231-233与RedisAdapter.py:94-96确认user/session key范围。
- 后端矩阵：supported_dataset_database_handlers.py确认shared/Aura-dev/community存在；PostgresGraphSharedDatasetDatabaseHandler.py确认schema路径与准确环境变量；postgres_demo/adapter.py:1-9、104-116确认demo与固定写锁。
- 迁移与建库区别：runner.py:86-164和startup.py:289-324/386-451确认PG全局migration锁；因此报告没有泛化“所有锁只在本机”。这也不等于业务写与迁移已互斥。
- JWT：get_auth_secret.py:25-53确认三项FASTAPI_USERS_*完整配置名称与每进程随机fallback。

## 对方案的独立质疑与修订

执行分析agent独立审阅设计笔记及最终第5-6节，修订：

1. generation方案加入现有增量标记冲突，避免新版本因全部跳过而发布空索引。
2. 不只版本化图/向量：SQL provenance、Data状态、improve和反馈也受版本协议约束。
3. 固定单次查询manifest，旧版本回收等待读者退出，发布CAS同时检查tombstone。
4. 先停止/等待所有写入者再补偿，repair_pending可重试；兼容Python3.10。
5. fencing epoch来自dataset级持久所有权，不是各job自己的attempt编号。
6. 上传对象GC与引用采用需要同记录互斥；固定宽限期不能避免暂停进程恢复后的悬空引用。
7. Temporal与SQL队列为替代调度路线，明确workflow是执行权威；SQL不能独立重投/补偿同一workflow。

## 文档检查

- 最终报告486行，约59KB（后续微调可能略增），3个Mermaid图，6个闭合代码围栏。
- 检查86个本地链接：文件均存在，指定行号未越界。
- 未发现TODO、工具内部web引用标记或“见草稿”依赖。
- 未执行Mermaid渲染、pytest、外部服务/云资源操作；不能将静态文档检查说成集群验收。
