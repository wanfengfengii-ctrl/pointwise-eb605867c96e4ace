# 清岬观测日账

清岬区域观测中心的归集员提交测站报送及勘误，数仓值班员维护分区日账，接班人按号复领定稿和逐条出处。区划调整期间，值班员可以分段建立新版，同时继续供应现行账。

## 运行

使用 Python 3.11、标准库和两个 SQLite 文件。完整回归入口由运行控制台提供。直接用 unittest 也可从项目根目录运行同一原套件。

```sh
python3 -B check_project.py
python3 -B -m unittest discover -s tests -t .
python3 -B -m examples.demo
```

演示读取 `examples/batches.json` 和 `examples/rules.json`，在临时目录创建两个库。青站 7000 mL 先进入湾组并签存晨班。扫描暂停期间更正为 11000 mL，河站新增 3000 mL，现行湾组公布 14 L 并确认。回收原变更、关闭两库并只重开仓库后，新版按北组 11 L、南组 3 L 发布。晨班号仍交出湾组 7 L 和原回执，午班号可交出新版。演示核对读数、日序和出处，实际结果由 `POINTWISE_SCENARIO` 输出。

## 业务接口

以 `storage.connect(path)` 分别打开源库和仓库。写入入口拥有各自事务，应在连接空闲时调用。由调用方管理连接生命周期。`catalog.bootstrap.initialize_source(source)` 和 `initialize_warehouse(warehouse, RuleVersion(rule_id, station_regions))` 建表并首次登记初始口径。再次初始化保留原有口径、前沿、修订、候选及定稿。样例数据由演示显式导入。

| 入口 | 输入与结果 |
| --- | --- |
| `source.intake.submit(source, batch_id, day, station, reading_id, *, operation='add', ml=..., expected_revision=None)` | 返回带日、序号和修订身份的受理回执。`correct` 和 `withdraw` 引用活动旧修订。撤销省略 `ml`，新增和更正须传实测值或显式 `None`。同批同参复用原回执。 |
| `source.intake.submit_cross_day(source, correction_id, source_day, target_day, station, reading_id, *, expected_revision)` | 跨日勘误。核对源日活动行与 `expected_revision`，目标日须不同且目标行身份从未使用；复制源行读数和读次，不接收另一份读数。一次事务内建立源日撤回与目标日补录，各占该日实际下一日序，返回两侧坐标、批号和原件绑定。同号同参重投返回原回执，异参、过期修订或已用目标行拒绝且不留半账。`lookup_cross_day(source, correction_id)` 可在回收后重开读回同一绑定。 |
| `source.feed.read_changes(source, day, *, after=0, through=None)` | 按日序返回仍可供出的前后像，左开右闭。`reclaim_confirmed(source, day)` 回收已确认原载荷，受理回执仍可按批号查询；联合勘误成员在联合实收已提交且两侧均确认前保留。 |
| `daily.receive.advance(warehouse, source, batch)` | 连续接收真实源批，仓内成套提交行、合计及前沿后确认上游。重投原批返回原实收回执。若回送确认失败，可用原批重投，亦可通过 `lookup_delivery` 查询已提交回执并用其日和序号重送确认。联合勘误成员只能经联合入口接办，普通入口直接提交会被拒绝。 |
| `daily.receive.advance_cross_day(warehouse, source, day, sequence)` | 从联合勘误任一侧坐标接办，核对源端同单原件与两侧各自下一公布日序。一侧缺前批时返回待办且两日账均不推进；齐备时两日明细、合计、进度和联合实收一次提交。重投同一或另一侧返回原联合实收，不重复计量。`lookup_cross_day_delivery` 可查已提交的联合首执。 |
| `catalog.baseline.prepare(warehouse, source, day, cut, rule)` | 选择实际源切点并返回固定底表清单及口径身份。已有固定输入可重读，新切点须由当前源截面及仍能取得的批次核齐。如底表落后于公布前沿，准备时一并核齐已公布的后续材料。 |
| `refresh.jobs.refresh_step(warehouse, job_id, day, baseline_id, rule_id, *, action, limit=100)` | `start` 绑定具名候选，`scan` 提交一个底表段，`catch_up` 至多兑现 `limit` 个连续批，`activate` 裁决切换。`covered=None` 表示底表尚未核齐，`waiting` 表示切换此时不能提交。已激活作业重投返回原终态。 |
| `handoff.issue.issue(warehouse, number, day)` | 返回含定稿和回执的 `IssuedStatement`，同号同日再次签存复领原结果。 |
| `handoff.readback.read(warehouse, *, day=None, number=None)` | 二选一。按日获取 `PublishedSnapshot`，按号获取 `IssuedStatement`。`handoff.audit.lookup_provenance` 可按片区或站过滤固定出处，`verify_statement_totals` 可复核定稿的计量。 |

## 计量与状态

日期用 `YYYY-MM-DD`，非空身份按原字符串区分。行由日、站号、读次识别。新增按首次受理日序分配原序，勘误保持原序，撤销留下修订头。撤销后的身份仍保留，可为新的读次分配新的身份。批号在源库范围内唯一，日序从 1 起分日连续。底表切点 0 表示日初空表。

只接受能精确换算成整数升的整数毫升。布尔值、浮点数和非整升值会拒绝。有效读数包括零升，缺测有行及原始单位，贡献零升和零有效行。按指定固定口径逐批扣除前像、加入后像。日账按片区标识顺序返回，组内出处按源原序排列。只有缺测的片区有零升零行的账面记录，完全没有行的片区省略。新版映射须覆盖底表和需兑现批次中的站点。

固定底表和已登记口径按身份重读。仓库为已固定切点保存之后已公布的变化依据，候选与源库分离后仍可续办。扫描按段提交，暂停时释放写事务。同一日最多一个在建作业。切换以事务内的实际公布前沿裁决，成套提交新版口径、进度、分区、出处和作业结果。已定稿的交班件沿用签存时的截面与回执。

非法输入、冲突身份、过期修订、日期或序号不合规由 `ValueError` 或其业务子类报告。确认之前必须补齐日账缺批。缺少可核齐的底表材料会拒绝准备，换版追赶遇缺口会保留可证明前缀或报告 `MissingChangeMaterial`。未知日账或固定号由 `LedgerNotFound` 报告。已发布的空账可查、可签存。SQLite 提交失败会回滚该事务并报告原错误。仓库提交后源端确认失败时，已有本地实收和公布截面仍可查询，并可继续重送确认。

跨日勘误的两侧各自占源日、目标日的实际下一日序，一次源端事务同时建立源日撤回与目标日补录，仓库联合入口再成套公布两天。任一侧前批未到时两日账都不推进，其他日期照常供数。普通 `advance` 拒绝联合成员。仓库提交后源端确认失败时联合实收已生效，可重开并从任一侧坐标补认，重复接办只返回原实收。联合原件在联合实收提交且两侧均确认前不被 `reclaim_confirmed` 回收，回收后 `lookup_cross_day` 仍返回原绑定和原回执。

原回归使用独立临时文件库及固定输入。确定性工序间调度覆盖候选就绪后现行账抢先公布，SQLite 授权器拒绝一次实际提交以核对失败与重开。预期金额、计数、日序及来源由固定样例手算断言。
