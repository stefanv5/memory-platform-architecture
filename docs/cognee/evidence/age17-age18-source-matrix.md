# AGE 1.7 / 1.8 源码能力矩阵

固定树：1.7/PG17 = PG17/v1.7.0-rc0 / `e1467f12e0b1d15dd35d3ab93f057a7112d425b8`；另核对 **1.7/PG18 = PG18/v1.7.0-rc0 / `806fa2ebdb300b3e76ef30cdba61803babbf2683`**；1.8/PG18 = PG18/v1.8.0-rc0 / `e43dc1a12b78fba4acef9835b2b10379b8d243b4`。1.7 不限于 PG17；下表 1.7 功能结论在这两个已读包一致。本稿不将功能首次出现在本报告的版本当成真正首次发布版本。没有编译、运行回归或部署实例。

状态：**支持**指声明、解析/执行及回归有可核对证据；**缺失**指指定语法或内置函数在所审树未实现，并给出解析/注册边界；**未核验**用于无法由本轮源码证明的完整兼容性。所有“支持”均不是 Cognee adapter 已交付。

| Primitive | 1.7 / PG17 与 PG18 源码 | 1.8 / PG18 源码 | 对 Cognee 的性质与影响 |
|---|---|---|---|
| 普通 MERGE | 支持，grammar `MERGE path`，批量 UNWIND+MERGE 回归 | 支持，保留普通形式 | 必要基础之一；足以实现基本 upsert，仍需业务 ID 唯一性和并发协议 |
| MERGE ON CREATE SET / ON MATCH SET | 指定语法缺失：merge 产生式到 path 结束，没有 action 分支 | 支持：grammar、transformer、executor 与回归均有 | 新便利能力，可减少创建/匹配分流语句；不是“1.7 无法更新图”，也不等于并发唯一性保证 |
| SET 单属性、SET += map、REMOVE 属性 | 支持 | 支持 | feedback/truth/valid_to/来源维护的必要基础；不是 1.8 才有局部更新 |
| SET null / map 中 null | 支持删除属性语义；`+=` 保留未提及键、覆盖新值后去掉 null 键 | 相同基本语义，有新增 MERGE action 表达式执行路径 | 必须处理字段归属，不能把 null 当作“保留旧学习状态” |
| UNWIND 列表批处理 | 支持，含重复输入 MERGE 回归 | 支持 | 批量接口基础；批量重复/并发失败仍需适配器测试 |
| SQL PREPARE + Cypher 参数 map | 支持，第三参数 `$1` 绑定、Cypher 内 `$var_name` | 支持同一路径 | 安全传值基础；不意味着任意函数位置、整张 CREATE map、查询文本/标识符都可直接绑定 |
| 固定跳 MATCH / 有界 VLE | 支持，已有 SQL SRF 与 DFS | 支持，增加 endpoint 标量、缓存/邻接等优化 | 必要图查询基础；1.8 主要是执行优化，不能把 1.7 标成不支持多跳 |
| SQL `age_shortest_path` / `age_all_shortest_paths` | 指定内置函数缺失：安装 SQL 无声明，VLE 实现无该 SRF | 支持，返回 SETOF agtype，标准路线无权 BFS | 可选新算法能力；不是普通邻域必须依赖的接口 |
| Cypher `RETURN shortest_path(a,b)` / `all_shortest_paths(a,b)` | 指定内置入口缺失 | **支持**：函数名解析到 age_*，自动注入 graph；已有 RETURN 回归 | 必须补入报告，不能说 1.8 只有外部 SQL 调用 |
| Neo4j `MATCH p=shortestPath((a)-[*]->(b))` | 本树无该模式产生式/同名内置函数 | 本树仍无该模式产生式；下划线函数不是此语法 | Neo4j 语法兼容缺口；不能原样搬 prompt。用户自定义 SQL 同名函数不属于内置兼容 |
| CALL / YIELD | 支持可调用函数路线，有 qualified SQL function 回归 | 支持 | 有调用语法不代表有 Neo4j procedure/plugin 生态 |
| APOC/GDS 内置兼容包 | 缺失：本树没有注册所需 APOC/GDS 函数；普通 CALL 是解析现有 PG/AGE 函数 | 同样缺失；部分功能可由 AGE 自有函数替代，不代表兼容包存在 | Neo4j 专属依赖，需重写或能力门禁。不可由“支持 CALL”推导存在 `apoc.merge.relationship` |
| RLS 的普通 Cypher 查询及 DML 支持 | 已有 executor policy/WITH CHECK、SET/DELETE/MERGE 路线与 security 回归 | 有对应实现与回归 | 可做 PG 权限设计，但不是 Cognee tenant/ACL 自动完成；全 VLE/最短路缓存路径的 RLS 完整性**未核验** |
| CSV file load + 已启用 RLS | 显式拒绝，提示改用 Cypher CREATE | 保留该限制 | 导入设计边界，不应矩阵简写“所有路径支持 RLS” |
| 自动 vertex ID / edge endpoint 索引 | 新 label 创建已有 vertex ID 主键索引、edge start/end B-tree | 保留 | 已有关系存储能力，不是 1.8 新增“全部索引” |
| properties GIN、属性表达式 B-tree | 手动建索引与计划回归已有 | 同样支持 | 需按实际 AGE agtype 表达式建索引；不自动给全部业务属性建索引 |
| 按内部 ID 水合节点的 index scan | 该 helper 使用扫描路线（前轮差分确认） | `get_vertex` 优先找可用 B-tree，否则 scan | 性能改进；不能扩张成 1.7 完全不能使用索引 |
| 指定 PG major 编译兼容 | 已核对分别针对 PG17、PG18 的两个 1.7 包；存在实际 PG API 适配差异 | 此固定包目标 PG18；有 DSM/SHMEM 的 PG_VERSION_NUM 条件分支。其他 major 未编译核验 | C 扩展 ABI/源码包部署问题，不是 Cypher primitive 差异；无显式拒绝 guard 不等于跨 major 可编译 |

## 可核对的关键证据

**MERGE 的正反证据。**1.7 `src/backend/parser/cypher_gram.y:1133–1143` 只有 `MERGE path`，并非通过“搜索不到测试”来断言 action 缺失。1.8 `:1191–1256` 增加 `merge_actions_opt` 及 `ON MATCH SET`、`ON CREATE SET` 产生式；`cypher_clause.c:8435–8456` 构造两套 update information；`executor/cypher_merge.c:359–376` 按新建/匹配分支执行。1.8 `regress/sql/cypher_merge.sql:936` 起有专项回归。[1.7 grammar](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/src/backend/parser/cypher_gram.y#L1133)、[1.8 grammar](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/parser/cypher_gram.y#L1191)、[1.8 executor](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/executor/cypher_merge.c#L359)。

**更新与 null。**1.7 `executor/cypher_set.c:468–518` 决定属性删除、使用原 properties 做 `+=` 合并，并去除 null；1.8 对应 `:528–610`。两版 `regress/expected/cypher_set.out:854–874` 共同证明 `{name:'Rob',role:NULL,age:47}` 会删 role、保留 city；空 map 不改属性。REMOVE 仅指属性删除，不把它扩张为完整 Neo4j 标签管理。[1.7 执行](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/src/backend/executor/cypher_set.c#L468)、[预期结果](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/regress/expected/cypher_set.out#L854)。

**批量与参数。**两版 `regress/sql/cypher_merge.sql:680` 起有 UNWIND+MERGE；`regress/sql/cypher_set.sql:144–153` 定义 PREPARE，并两次传不同 JSON 参数执行。支持参数传值不等于允许参数替代语法结构；1.7 `cypher_clause.c:6407` 还明确拒绝 CREATE clause 整体 properties 作为 parameter 的一种形式。[批量回归](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/regress/sql/cypher_merge.sql#L680)、[参数回归](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/regress/sql/cypher_set.sql#L144)。

**VLE 已有，shortest 是不同新增。**1.7 `sql/agtype_typecast.sql:73–93` 已声明两种 age_vle 重载；1.8 保留并增加输出 endpoint 列。1.7 此声明段直接接 `age_build_vle_match_edge`，全安装 SQL 未注册 age_shortest_path/all_shortest_paths；1.8 `:101–129` 明确注册两个新 SRF。前轮深读已确认其 BFS、min_hops fallback 及预物化边界，本稿不重复展开。[1.7 VLE 声明](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/sql/agtype_typecast.sql#L73)、[1.8 shortest 声明](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/sql/agtype_typecast.sql#L101)。

**1.8 Cypher 最短路函数是确定支持，不应再标未核验。**`cypher_expr.c:2217–2287` 把未限定函数名转成 `age_` 前缀，并对 shortest_path/all_shortest_paths 注入当前 graph。`regress/sql/age_shortest_path.sql:418–456` 显式使用 `MATCH (a...), (c...) RETURN shortest_path(a,c)`。但其 `path` 产生式 `cypher_gram.y:1350–1397` 仍是 anonymous_path 或变量等于 anonymous_path，没有 Neo4j camelCase pattern-wrapper。函数名转换只加前缀并转小写，不会将 `shortestPath` 自动变为 `shortest_path`；安装 SQL 也没有 `age_shortestpath` 别名。因此“AGE 下划线函数可用”与“Neo4j 最短路原语法不可直接兼容”可同时成立。[函数解析](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/parser/cypher_expr.c#L2217)、[Cypher 回归](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/regress/sql/age_shortest_path.sql#L418)、[path grammar](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/src/backend/parser/cypher_gram.y#L1350)。

**CALL 与插件。**两版 `regress/sql/cypher_call.sql:30–50` 先创建普通 SQL 函数，再从 Cypher CALL 调用；另有 sqrt/YIELD 回归。1.8 `cypher_expr.c:2248–2252` 将 qualified function 原样交给 PG 解析，`:2262–2305` 检查已有 AGE/其他 extension 函数。此路线不是加载 Java APOC/GDS 插件；本树注册 SQL 未定义 Cognee 使用的 apoc.coll.toSet、apoc.create.addLabels、apoc.merge.relationship。1.8 age_subgraph SQL 注释提到 GDS 对应概念，也不是注册 gds.* 兼容实现。[CALL 回归](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/regress/sql/cypher_call.sql#L30)。

**RLS 不能说成 1.8 才支持，也不能说全路径已证明。**1.7 `cypher_create.c:126–129` 设置 WITH CHECK，`cypher_utils.c:280–284` 调用 ExecWithCheckOptions；1.8 对应 `:128–132`、`:377–380`。两版 `security.sql:666–684` 有不同角色的 SELECT 行过滤，后续测试 INSERT/UPDATE/DELETE/MERGE 和边策略。两版 `utils/load/age_load.c:178–190` 对已启用 RLS 的 CSV file load 显式 ERROR。前轮的 VLE global cache 是专门的 heap/邻接访问路径，本轮没有完成其全路径 RLS/角色切换审计，矩阵必须保留此限定。[1.7 CREATE RLS](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/src/backend/executor/cypher_create.c#L126)、[角色过滤测试](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/regress/sql/security.sql#L666)、[导入限制](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/src/backend/utils/load/age_load.c#L178)。

**索引不是新旧版二元开关。**1.7 `label_commands.c:425–434` 与 1.8 `:427–436` 均在新建 label 时建立 vertex id 主键、edge start/end 索引；两版 `regress/sql/index.sql:214、263、296、335、391` 均有手动 GIN/属性表达式 B-tree。1.8 的 `agtype.c:6155–6193` index scan 改进仅是具体 helper 的路线变化。索引是否被 planner 选择仍需计划验证。[1.7 自动索引](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/src/backend/commands/label_commands.c#L425)、[1.7 属性索引](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/regress/sql/index.sql#L214)。

**PG 编译边界。**1.7 `Makefile:138–140` 和 1.8 `:276–278` 均使用 PG_CONFIG 获取 PGXS。没有在所审 1.7 src/Makefile 发现限定只允许 PG17 的 `PG_VERSION_NUM/#error`，不能捏造这样的 guard；1.8 `age.c:30、73`、`age_global_graph.c:1824–1841` 的 `<170000` 判断用于 SHMEM/DSM 路线，不能当成全面跨 major 兼容保证。本轮没有将这两包分别对其他 PG headers 编译或运行；完整 major 矩阵应由对应包/构建证据另外给出。[1.7 PGXS](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/Makefile#L138)、[1.8 PGXS](https://github.com/apache/age/blob/e43dc1a12b78fba4acef9835b2b10379b8d243b4/Makefile#L276)。

补核 1.7/PG18 与 1.7/PG17：`src` 差分只有 11 文件、51 增/16 删，已阅读全部 diff；`sql` 目录无差异，`regress` 仅 cypher_match 预期计划变化，grammar、cypher_expr、VLE/global graph、RLS helpers 和所列 SQL 函数一致。实质改动包括 `expandRTE` 增加 `VAR_RETURNING_DEFAULT`（PG18 cypher_clause.c:2732）、`ExecInitRangeTable` 增加参数（age_load.c:812）、TupleDescAttr/头文件调整，以及 SET 已打开索引的生命周期防护（cypher_set.c:105–189）。因此两个 1.7 包的本表功能一致，但不能拿 PG17 包不改源码直接推断可编译到 PG18。[PG18/1.7 parser 适配](https://github.com/apache/age/blob/806fa2ebdb300b3e76ef30cdba61803babbf2683/src/backend/parser/cypher_clause.c#L2732)、[PG18/1.7 SET 索引处理](https://github.com/apache/age/blob/806fa2ebdb300b3e76ef30cdba61803babbf2683/src/backend/executor/cypher_set.c#L105)。

## 对现有报告应补的唯一实质性表述

前稿谨慎只确认 SQL shortest SRF，没有确认 Cypher 函数入口；本轮已经发现并读到 parser 和测试，主报告应补充：**1.8 已支持 AGE Cypher RETURN shortest_path/all_shortest_paths，自动注入图名；仍不能原样兼容 Neo4j shortestPath(pattern) 语法。**这比继续标“仅 SQL 支持”或“Cypher 未核验”更准确。

## 阅读范围

复用前轮两版 VLE、global graph、fixed-path parser/optimizer/index 读取。本轮补读两版 MERGE/SET/path grammar、1.8 函数解析路由及 shortest Cypher 回归、普通/条件 MERGE 执行分支、SET null 合并处理及预期输出、参数回归、CALL 回归、RLS 初始化/WITH CHECK 与代表性角色测试、CSV RLS 拒绝分支、建 label 索引、Makefile PGXS。只对这些有限目标报告证据；未通读全部 parser、安全代码或所有测试，不宣称全仓覆盖或运行通过。
