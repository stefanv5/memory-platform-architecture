# AGE 业务唯一性：默认机制、并发边界与适配器设计

范围固定为 AGE PG16/v1.6.0-rc0 `2db2f060c4c9265a14d40f007eb8c56febf31e4c` 与 PG18/v1.8.0-rc0 `e43dc1a12b78fba4acef9835b2b10379b8d243b4`。本次只读源代码和上游回归预期，没有运行双会话实验。下文“源码事实”“推导”“待测试”分别标明证据级别。

## 可以直接用于主报告的结论

**PG 能实现业务唯一约束，AGE 的普通写入也会执行这些约束；需要设计的是约束哪些字段、在哪些 label 表之间生效，以及约束冲突后如何恢复。不能把这项工作解释成 PG 缺少唯一约束。** AGE 默认内部 `graphid` 唯一，不等于 Cognee 放入 `properties.id` 的 UUID 唯一。`MERGE` 是匹配已有图模式、否则创建的执行机制，不自动生成业务 UUID 唯一索引，也不是带冲突重试的 SQL `INSERT ... ON CONFLICT`。

## 1. 内部 ID 与业务 UUID 是两个字段

**源码事实：** `graphid` 为 int64，由 label ID 和 48 位 entry ID 编码；每个 label 创建自己的 sequence，label 创建时分配 label ID。业务属性存入独立 `properties agtype` 列。

- [1.8 graphid.h:29–60](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/include/utils/graphid.h#L29)。
- [1.8 label_commands.c:330–355](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/commands/label_commands.c#L330)：sequence、label ID。
- [1.6 label_commands.c:446–495](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/commands/label_commands.c#L446)：vertex/edge 的内部 `id` PRIMARY KEY；`properties` 只有 NOT NULL 和默认值。
- [1.8 label_commands.c:427–435](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/commands/label_commands.c#L427)、[497–535](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/commands/label_commands.c#L497)、[566–615](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/commands/label_commands.c#L566)：vertex 内部 ID 主键索引，edge ID 主键，非唯一的 start_id/end_id 索引；没有 properties.id 默认唯一约束。

**推导：** 两个节点可拥有不同内部 graphid，同时都有 `properties.id = "同一个 UUID"`。如果适配器把业务 type 映射成 label，分别写入 `:Document {id: U}` 和 `:Entity {id: U}`，两张 label 表之间也不会自动去重。这是需要适配器选择的身份规则：UUID 在每个 label 唯一，还是在整个 dataset/graph 唯一。

AGE label 确实采用 PG 表继承构造：[两版相同行附近，1.8 label_commands.c:407](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/commands/label_commands.c#L407)。**不能只在 `_ag_label_vertex` 父表建一个 UNIQUE 就认为覆盖所有 label**；PG 明确说明唯一索引只约束单张物理表，父表及各子表分别加索引也不构成跨表唯一。[PG 18 inheritance caveats](https://www.postgresql.org/docs/18/ddl-inherit.html#DDL-INHERIT-CAVEATS)。

## 2. 已有唯一索引证据，但不是现成业务 UUID 索引测试

**源码事实：** 两版上游回归都建立 `CREATE UNIQUE INDEX ... ON ...idx(properties)`，随后通过 Cypher CREATE/SET 制造重复，预期得到 duplicate key 错误。这证明 AGE Cypher 写入尊重该 PG UNIQUE 索引。

- [1.6 regress/sql/index.sql:37–71](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/regress/sql/index.sql#L37)。
- [1.8 regress/sql/index.sql:35–69](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/regress/sql/index.sql#L35)，[expected/index.out:43–84](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/regress/expected/index.out#L43)。
- 普通属性表达式索引另有回归：[1.8 index.sql:263–264](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/regress/sql/index.sql#L263)，1.6 对应 L267–268。

**必须保留的限定：** 前者对整个 properties map 去重，后者是非 UNIQUE 属性表达式索引。本次检索两版 `regress/sql` 的 CREATE UNIQUE INDEX，没有找到直接以业务 UUID 属性表达式建 UNIQUE 的正向回归。不能把这两类测试合写成“业务 UUID 唯一表达式索引已经上游验证”。

**反例推导：** `{id: U, name: "A"}` 与 `{id: U, name: "B"}` 是不同 map；整个 properties UNIQUE 不能禁止它们共享 UUID。生产适配器应约束 UUID 提取表达式，而不是照抄整个 map 索引。

可验证的 schema 方案示意（尚未在目标 PG/AGE 实例执行；需同时限制 id 必填、规范 UUID 表示和不可随意修改）：

```sql
CREATE UNIQUE INDEX cognee_node_business_id_uq
ON graph_schema."CogneeNode"
((ag_catalog.agtype_access_operator(properties, '"id"'::agtype)));
```

UUID 的大小写/类型/缺失与 agtype null 规则要统一；不能仅靠 UNIQUE 自动实现必填。索引表达式、CHECK 表达式及查询谓词匹配须在选定版本验证。避免为兼容 1.6 直接假定新增 generated column 路线与 1.8 等价。

## 3. MERGE 的并发边界

**源码事实：** 两版 MERGE 会打开目标 label 表并持有 `RowExclusiveLock`；这是普通并发写入使用的表锁，不是对业务 key 的互斥锁。[1.6 cypher_merge.c:154–164](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/executor/cypher_merge.c#L154)、[1.8:159–169](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/executor/cypher_merge.c#L159)；其自兼容性见 [PG explicit locking](https://www.postgresql.org/docs/18/explicit-locking.html#LOCKING-TABLES)。

两版用 `created_paths_list` 查找本次 custom scan 已创建的路径：[1.6:517–550](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/executor/cypher_merge.c#L517)、[1.8:594–628](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/executor/cypher_merge.c#L594)。这是执行节点私有链表，不能协调两个 PG backend。1.6 的注释明确说明它处理本次扫描看不到刚插入元组的问题（L625–641）；1.8 对 UNWIND 重复输入的回归也只是同一语句内去重（[cypher_merge.sql:674–689](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/regress/sql/cypher_merge.sql#L674)）。

1.8 查询子计划、判定路径缺失、查询私有链表、创建路径的连续路径见 [cypher_merge.c:693–750](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/executor/cypher_merge.c#L693)，1.6 对应 L620–674。插入最终调用普通 `table_tuple_insert`，随后 `ExecInsertIndexTuples`；没有走 speculative insertion / ON CONFLICT，不捕获 unique violation 并重跑匹配：

- [1.6 cypher_utils.c:242–269](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/executor/cypher_utils.c#L242)。
- [1.8 cypher_utils.c:370–395](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/executor/cypher_utils.c#L370)。

**并发推导，未冒充实测：** T1、T2 同时 MERGE 尚不存在的 UUID；两者的匹配均未发现节点，各自链表也为空，分别分配不同 graphid 并插入。没有 UUID 唯一索引时，默认 graphid 主键并不阻止两个业务重复节点；有 UUID 唯一索引时由 PG 拒绝重复，失败请求仍需恢复事务并在适当的新快照下重新匹配。不能把“PG 拒绝重复”写成“所有请求自动成功返回同一节点”。

1.8 `ON CREATE SET` / `ON MATCH SET` 只在已判定的分支应用更新（上述 L730–750），不会自动建业务索引、跨 label 合并 UUID、串行化并发缺失检查，或在 unique violation 后切换为 ON MATCH。

## 4. 业务边和平行边

**源码事实：** edge 表有独立内部 ID；start_id/end_id 是 NOT NULL，不是唯一的端点组合，1.8 新增的端点索引也显式 `unique=false`。因此默认可以保存两条端点相同、label 相同、内部 ID 不同的边。

这是正常的属性图多重边能力，**不是 AGE 数据库 bug**。只有当 Cognee adapter 的业务契约定义同一 `(source UUID, target UUID, relationship type)` 应是同一条边时，才需要补充约束。

- 若关系类型分别映射为独立 edge label：每张关系 label 表可以设计 `UNIQUE(start_id, end_id)`，前提是业务不允许该类型平行边。
- 若所有关系进入统一 `CogneeEdge` label：设计 `UNIQUE(start_id, end_id, relationship_name 提取表达式)`。
- 必须先保证 UUID→内部 graphid 唯一，否则两个重复 source 节点可让内部端点组合不同，绕过业务三元组的意图。
- 定义方向、自环、不同 dataset 重复 UUID 是否允许；有向边不能擅自排序端点。若业务允许平行边，需要稳定的边业务 ID，而不是端点三元组唯一。

上述是待 PoC 验证的 adapter/schema 方案，不是 AGE 自动提供的默认约束。

## 5. 推荐设计与失败处理

最小复杂度方案是在每个 dataset graph 内统一使用一个节点 label，以 `properties.type` 表示业务类型；给该表的规范 UUID 表达式建立唯一索引。这样 label 类型变化不会把同一 UUID 移到另一张表，graph 内唯一约束的范围清楚。代价是按 type 检索要另建匹配索引，不能再把业务类型完全等同于 AGE 物理 label。

若必须保留按 type 分 label，应另设计一个共享 PG 身份登记表，例如主键 `(graph_scope, business_uuid)`，登记唯一 label/internal graphid；登记与 AGE Cypher 写入须同连接、同事务完成，并规定删除、重试、类型冲突与迁移行为。这是额外一致性协议，不能只建一张登记表就声称完成。图 DML 优先通过 AGE 维护，登记表可以使用普通 SQL upsert；本草稿不推荐绕过 AGE 直接改图 label 行。

写入应只把稳定身份字段放进 MERGE pattern，然后按约定更新普通属性。如果把 name/时间戳等可变字段放入 MERGE pattern，现有 UUID 但属性变化会造成匹配失败；唯一索引虽然阻止重复，但重复同一错误请求不会自然成功。

adapter 需要在 unique violation 后回滚失败事务或受控 savepoint，再以新快照重新匹配；区分并发同 key 冲突和永久非法输入，限定重试次数。若采用更强隔离，serialization failure 也需事务重试。批量请求还要定义事务原子范围，避免只重试一半产生不完整状态。对按业务 key 加 advisory lock 的优化，要求所有 writer 遵守同一协议；它不能代替数据库唯一约束。

验收至少覆盖：双会话同 UUID 首次 MERGE、同 UUID 不同 name、同 UUID 不同 type label、批量输入重复、相同边三元组并发写、属性更新碰撞、删除后重建、缺失/null/不同 UUID 表示、冲突后重试返回唯一对象。检查最终业务节点/边数与关联端点，不能只看无异常。

## 阅读边界

定向精读了两版 MERGE 的锁初始化、created_paths_list 查找、缺失路径创建分支，以及普通插入 helper 全函数；两版 label 列定义、继承构造，1.8 自动索引函数及 sequence/label ID 创建；index.sql 的 whole-properties UNIQUE 与属性表达式索引测试、预期 duplicate-key 输出，1.8 同语句重复 MERGE 测试。没有宣称覆盖整个 executor，也没有运行并发/索引 DDL 实验；跨会话竞态是由已审计执行路径和 PG 锁/唯一索引语义得出的推导。
