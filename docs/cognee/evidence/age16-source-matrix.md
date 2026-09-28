# AGE 1.6：PG14 / PG15 / PG16 / PG17 正式 tag 的源码能力矩阵

日期：2026-09-28。本次直接读取三个本地正式 tag checkout，以 PG16 为主读，对 PG14/15 比对文件摘要、grammar diff 和指定 expected 区段。没有构建数据库或执行回归；expected 是项目已有预期输出，不是本轮运行结果。下列确认的是数据库 primitive，不是 Cognee adapter 已完成。

## 1. 身份与构建目标

| PG | 正式 tag | 本地 HEAD（已运行 git rev-parse） | age.control:18 |
|---|---|---|---|
| 14 | PG14/v1.6.0-rc0 | 41c08296a4b692adf31ed9507a9a25dad6f9f67f | default_version = 1.6.0 |
| 15 | PG15/v1.6.0-rc0 | fa1af8de99d74d32e131c19b62cf580d396eb0ae | default_version = 1.6.0 |
| 16 | PG16/v1.6.0-rc0 | 2db2f060c4c9265a14d40f007eb8c56febf31e4c | default_version = 1.6.0 |

**必须纠正“Makefile 都有 PG_VERSION_NUM guard”的假设：这三个正式1.6 Makefile逐字相同，未包含这种 guard。**实际137–139行通过 PG_CONFIG 获取 PGXS；19行指定 age--1.6.0.sql，81–83行读取 sql/sql_files，153–154行拼接扩展安装SQL。因此目标PG身份依赖对应正式发行/tag和各分支PG API适配，不能捏造guard作证，更不能从没有guard反推可跨PG主版本直接构建。

源码：[PG14 age.control](https://github.com/apache/age/blob/41c08296a4b692adf31ed9507a9a25dad6f9f67f/age.control#L18)、[PG15 age.control](https://github.com/apache/age/blob/fa1af8de99d74d32e131c19b62cf580d396eb0ae/age.control#L18)、[PG16 Makefile](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/Makefile#L137)。

PG17/1.6亦是正式发布过的组合：tag `PG17/v1.6.0-rc0`，HEAD `54905a09bf8462f22a87c3adfd2ab5752e5c1e71`，age.control:18为1.6.0。其源码补核验见第5节；不能因为当前下载目录已换成1.7，就从AGE1.6支持范围删除PG17。

## 2. 逐项矩阵

以下代码行号以 PG16 固定提交 `2db2f060c4c9265a14d40f007eb8c56febf31e4c` 为准。三列相同结论并非从版本号推定，比较依据见第3节。

| Primitive | PG14 1.6 | PG15 1.6 | PG16 1.6 | 直接源码与expected证据、限制 |
|---|---|---|---|---|
| CREATE节点/边，MATCH读取 | 有 | 有 | 有 | grammar CREATE pattern 1090–1099；expected/cypher_merge.out:1406–1436 创建节点边后固定跳MATCH取回；cypher_delete.out:58 创建边后读取。不是仅“有token” |
| 普通MERGE | 有 | 有 | 有 | grammar:1229–1239 MERGE path；expected/cypher_merge.out:1406–1436 对重复foo/bar输入复用节点边，最终MATCH为2行。这个单会话回归不证明并发唯一性 |
| UNWIND批量输入+MERGE | 有 | 有 | 有 | grammar:1070–1084 UNWIND expr AS var；expected/cypher_merge.out:1406输入4元素返回4行但仅2条不同图模式，1418再次执行复用 |
| SET单属性、SET整map | 有 | 有 | 有 | grammar:1131–1143 expr = expr；expected/cypher_set.out:846–853 非map整对象赋值报错，map语义与任意值赋值须区分 |
| SET += map | 有 | 有 | 有 | grammar:1144–1155明确is_add=true；expected/cypher_set.out:854–875旧city保留、name修改、age新增、role=NULL移除，空map不改变属性。不能说 += 永远保留所有已有字段或忽略输入NULL |
| REMOVE属性 | 有 | 有 | 有 | grammar:1158–1195将移除项变为NULL赋值；expected/cypher_remove.out:69–85显示属性移除后再读取仍已移除。不是直接删除实体 |
| DELETE / DETACH DELETE | 有 | 有 | 有 | grammar:1201–1224显式detach布尔；expected/cypher_delete.out:64–67普通DELETE有边节点报错，93–97 DETACH成功。支持关联边清理，不等于业务来源感知删除 |
| properties(node/edge) | 有 | 有 | 有 | sql/age_scalar.sql:72–78安装age_properties；expected/expr.out:3115–3138返回节点/边map，3140起NULL行为。函数声明和实际回归输出双证据 |
| list、IN成员判断 | 有 | 有 | 有 | expected/expr.out:158–164异构list包含1得到true；list_comprehension.out:46–61列表生成、索引、slice结果；只据具体受测形式，不承诺所有Neo4j类型强制转换一致 |
| list comprehension（过滤/映射） | 有 | 有 | 有 | grammar:2213–2239含四种IN/WHERE/映射组合；expected/list_comprehension.out:64–85返回过滤后的列表及平方映射。可用来实现部分数组处理，但不是APOC全库替代 |
| coalesce | 有 | 有 | 有 | grammar:1896起COALESCE专门产生式，expr.c:2100附近做共同类型处理；expected/expr.out:3161–3192测试整数、浮点、字符串、列表，第一个非NULL值被返回 |
| prepared query参数 | 有，专用通道 | 有，专用通道 | 有，专用通道 | sql/age_query.sql:49–54 cypher(name,cstring,agtype)；analyze.c:529–544第三参必须PG Param，否则明确错误；expr.c:800–833从参数agtype map取键；expected/cypher_match.out:641–649 PREPARE+EXECUTE使用$props命中1节点。不能把任意第三参字面量或Bolt参数协议当成相同能力 |
| 固定跳模式 | 有 | 有 | 有 | expected/cypher_merge.out:1430–1436 MATCH(u)-[e]->(v)返回真实固定跳结果；由pattern/path grammar表达。此处不审计JOIN与性能 |
| 变长路径VLE、跳数界 | 有 | 有 | 有 | grammar:737–761解析*range并检查下界≤上界；sql/agtype_typecast.sql:73、85安装两个age_vle重载；expected/cypher_vle.out:146–162含0..、1..、1..200；470起零跳到1跳。不是1.8才有VLE |
| MERGE ON CREATE SET / ON MATCH SET | **缺该语法分支** | **缺该语法分支** | **缺该语法分支** | 直接读完整MERGE产生式1229–1239，只有MERGE path，没有actions字段/产生式；同三份grammar该段一致。这是明确语法缺口，不只是关键字搜索为空。普通MERGE后SET不能自动获得按创建/匹配分别执行的语义 |
| 新版专用age_shortest_path / age_all_shortest_paths SQL API | **正式安装不提供这两个API** | 同左 | 同左 | 枚举Makefile引用的sql/sql_files全部安装函数声明，每包336条，均无这两个声明；agtype_typecast.sql安装的是age_vle等。未声明不等于无法用VLE/SQL在应用组合求最短，更不能称完全无法表达最短路径 |
| Neo4j APOC/GDS原有过程 | **本包不提供；不可直接兼容** | 同左 | 同左 | 相同安装函数全集无APOC/GDS声明；grammar:1750–1765函数最多schema.function，重复限定报function already qualified，因此apoc.coll.toSet等三段原名还面临语法限制。可自定义PG函数或写适配算法，但那是新增实现，不是内置 |
| RLS强制执行完整图读写 | **不能依赖；未核验完整保证** | 同左 | 同左 | 1.6插入函数utils.c:242–269检查ExecConstraints后直接table_tuple_insert及索引写入，没有RLS WITH CHECK调用；src/sql/回归未发现对应RLS覆盖。本轮不能据此声称所有SELECT绕过RLS或绝对不能配置RLS；主报告应写“未具备已确认的1.7显式RLS执行路径，需升级或专项安全验证”，不是把PG原生RLS能力直接算给AGE |

主要固定源码入口：

- [grammar](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/parser/cypher_gram.y#L1070)
- [MERGE/UNWIND expected](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/regress/expected/cypher_merge.out#L1405)
- [SET += expected](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/regress/expected/cypher_set.out#L854)
- [DELETE正反例](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/regress/expected/cypher_delete.out#L63)
- [prepared参数限制](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/parser/cypher_analyze.c#L529)
- [prepared expected](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/regress/expected/cypher_match.out#L641)
- [list comprehension expected](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/regress/expected/list_comprehension.out#L46)
- [VLE声明](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/sql/agtype_typecast.sql#L73)
- [实际安装SQL清单](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/sql/sql_files)
- [1.6插入函数](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/executor/cypher_utils.c#L242)

## 3. 哪些跨PG比较真正做过

### 文件逐字相同（SHA-256比较）

三者的 Makefile、age.control、sql/sql_files、sql/age_query.sql、sql/age_scalar.sql、sql/agtype_typecast.sql逐字相同。以下每一项的SQL与expected两份文件都分别逐字相同：cypher_merge、cypher_set、cypher_remove、cypher_delete、cypher_vle、cypher_match、list_comprehension。

这支持把表中对应数据库primitive结果列为同一能力等级，但不证明整个执行器实现逐字相同或所有边界行为等价。

### 并不相同的源码和回归

- cypher_gram.y：PG15/16 diff仅两个subquery产生式缩进；PG14与16有PG AST Value→String/Float、A_Const字段等兼容差异，以及do_negate_float处理正负号辅助逻辑差异。主读的CRUD、MERGE/UNWIND、SET/REMOVE/DETACH、list comprehension语法分支相同；不能声称整份grammar或数字边界行为完全相同。
- sql/agtype_access.sql：PG14与15/16差异是两个函数声明的缩进，语义声明相同。
- cypher_expr.c、cypher_clause.c、cypher_analyze.c和cypher_utils.c有PG适配差异，本轮未将全文相同作为依据。prepared能力用三者相同expected加主读实际参数通道确认。
- expr.sql / expr.out全文不相同。因此另截取并比较properties区段、coalesce区段、IN成功区段：三者分别SHA256前12位 `af35070b3b76`、`51eac3591eda`、`ac5915588202`，逐段相同，行号也相同。表中只合并这些已核对区段，不把整个表达式实现泛化为等价。

## 4. 结论与未知项

AGE1.6三个正式PG分支已经提供通用图CRUD、批量输入、局部map更新、属性列表操作、prepared传参和多跳模式。它们不足之处应具体列为：没有MERGE分支actions语法、不附带1.8新专用最短路API、不附带APOC/GDS兼容过程，以及不能从本轮证据承诺完整图RLS强制执行。不能把这些具体缺口误写成“1.6没有基本建图/查图能力”。

未验证运行态权限、RLS策略实际结果、并发MERGE唯一性、跨事务可见性、所有参数类型、输出顺序稳定性、驱动映射及Cognee高级接口闭环。为满足这些业务语义仍需adapter与测试；数据库primitive存在不等于适配完成。

阅读范围：PG16 grammar定向1070–1239、737–761、1728–1770、1896附近、2213–2239；analyze.c529–548、expr.c800–833；utils.c220–269完整插入包装及实现；所列SQL安装声明和manifest；expected中对应成功/失败区段。PG14/15进行了文件摘要与上述差异/指定区段比较。没有全仓逐行审计，没有虚报覆盖率，没有阅读Cognee或性能内核。


## 5. 后续补核验：PG17 的 AGE1.6正式分支

实际 checkout HEAD已确认：`54905a09bf8462f22a87c3adfd2ab5752e5c1e71`。Makefile、age.control、sql/sql_files、age_query.sql、age_scalar.sql、agtype_typecast.sql与PG16逐字相同；因此也**没有Makefile PG_VERSION_NUM主版本guard**。这是正式PG17版本的源码，不是把PG16包强行装到PG17。

| 第2节能力 | PG17 / AGE1.6结论 | 比对依据 |
|---|---|---|
| CREATE/MATCH/MERGE/UNWIND | 有，同等级 | cypher_merge SQL与expected逐字相同，grammar unwind1054、merge1213产生式同义 |
| SET / SET += / REMOVE / DELETE / DETACH DELETE | 有，同等级 | 四份expected与PG16逐字相同；set_item1115、delete1185 |
| properties/list/IN/coalesce | 有，所审形式同等级 | expr.out全文有差异，但第3节三段哈希与PG16完全相同；age_scalar声明相同 |
| list comprehension | 有，同等级 | expected逐字相同；grammar2197对应四种产生式 |
| prepared传参/固定跳 | 有，同等级 | cypher_match.expected与PG16逐字相同，prepared正向结果641–649相同 |
| VLE | 有，同等级 | cypher_vle.expected、agtype_typecast.sql与PG16逐字相同 |
| ON CREATE SET / ON MATCH SET | 同样缺语法分支 | grammar1213–1223仍仅MERGE path |
| 两个专用shortest API / APOC/GDS过程 | 同样不随正式安装提供 | 相同manifest，实际枚举336个安装函数，未声明这些API；不排除自行实现 |
| 完整图RLS强制执行 | 不可依赖本轮保证 | 同样未发现1.7所引入的显式check_enable_rls/ExecWithCheckOptions路径；保持第2节限定 |

**但不能把四个1.6分支说成语法完全相同：**PG17 grammar:638–642为无RETURN子查询UNION构建 `make_subquery_returnless_set_op`，3036起辅助函数设置returnless_union=true；PG14/15/16对应grammar:652–656仍明确报错 `Subquery UNION without returns not yet implemented`。这是本轮完整grammar差异比较发现的真实功能差异；PG17还调整了qualified CALL产生式形式。此能力不在上面的Cognee基本primitive清单中，但必须作为“同版号仍有PG分支差异”的实例保留。

固定证据：[PG17 1.6 grammar](https://github.com/apache/age/blob/54905a09bf8462f22a87c3adfd2ab5752e5c1e71/src/backend/parser/cypher_gram.y#L638)、[PG16明确拒绝分支](https://github.com/apache/age/blob/2db2f060c4c9265a14d40f007eb8c56febf31e4c/src/backend/parser/cypher_gram.y#L652)。这里尚未执行两个版本的对照查询，不把语法差异扩大成所有相关子查询运行语义已测。

### RLS的跨版本正向对照（来自负责1.7审计agent的已读证据）

PG17/AGE1.7固定提交 `e1467f12e0b1d15dd35d3ab93f057a7112d425b8`：cypher_create.c:126–129调用check_enable_rls并设置INSERT WCO；cypher_utils.c:280–284执行ExecWithCheckOptions(WCO_RLS_INSERT_CHECK)。security.sql:666–684包含ENABLE/FORCE ROW SECURITY、owner=current_user策略和不同角色MATCH。与1.6所读插入函数的普通约束检查→直接写入构成具体代码对照，支持“1.6缺这条已确认的显式RLS插入强制执行路径”。不据此声称1.7所有VLE/cache路径都已做完整权限证明。

[1.7 INSERT RLS入口](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/src/backend/executor/cypher_create.c#L126)、[1.7 WCO执行](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/src/backend/executor/cypher_utils.c#L280)、[1.7 RLS回归](https://github.com/apache/age/blob/e1467f12e0b1d15dd35d3ab93f057a7112d425b8/regress/sql/security.sql#L666)。
