# Student Collector V2：D1–D5 架构决策契约

本 ADR 只补齐 PR #9 审查指出的五项契约。它不改变 V1 代码、V1 数据库或当前部署。

## D1：多模板项目、入口和版本指针

### 决策

一个 `CollectionProject` 可以有多个同时有效的 `CollectionEntry`。每个入口是一个稳定的公开采集地址，并且在任一时刻只指向一个已发布 `TemplateVersion`；项目自身只保留一个当前发布的 `ProjectConfigVersion` 指针，入口解析时使用该项目配置：

```text
CollectionProject 1--N CollectionEntry N--1 TemplateDefinition 1--N TemplateVersion
                                      |
                                      +-- published_template_version
```

表约束：

- `CollectionEntry(project_id, slug)` 唯一；`slug` 只允许小写 ASCII、数字和连字符。
- `CollectionEntry.published_template_version_id` 必须属于同一项目，并且状态为 `published`。
- 同一入口只有一个当前发布指针；项目不再使用含义不明确的单一 `current_template_version` 指针。
- 一个已发布模板版本可以被多个入口复用；入口自身的范围、说明和配置可以不同。
- 每个项目最多一个 `is_default=true` 的入口，V1 内置项目使用 `default`。

公开接口：

- V2：`GET/POST /collect/<project_slug>/<entry_slug>/`。
- V1：`/submit` 继续映射到内置项目的 `default` 入口和兼容学生预设，不让客户端提交任意模板 ID。
- `start` 由服务端按入口解析 `entry_id`、发布的 `template_version_id` 和当前 `config_version_id`，客户端只提交入口 URL 及文件清单。

历史绑定：

- `CollectionRun` 保存 `entry_id`、`template_version_id`、`config_version_id`、`template_schema_sha256`、`config_sha256` 和 `export_snapshot_json`。
- 入口切换版本只影响新建会话。已经创建的 `UploadSession`/`CollectionRun` 继续使用原模板和配置，直到完成、过期或被管理员取消。
- 停用入口禁止新会话，但不撤销已有私有结果链接、正在完成的会话或审计记录；已过期凭证仍按原安全规则拒绝。
- 停用模板版本不能被新入口引用；旧入口和旧会话仍可按 TTL 完成。模板版本只有在没有活动会话和活动入口引用时才能标记 `retired`。

## D2：动态唯一字段的索引和生命周期

### 决策

`RecordUniqueValue` 改为逻辑唯一声明，不能把 `template_version_id` 无条件放进唯一键。字段 key 是项目内稳定命名空间；字段语义变化必须使用新 key。

规范化表：

```text
RecordUniqueValue
  project_id
  field_key
  namespace_kind       # project | scope | template
  namespace_key        # * | 规范化范围 | template:<definition-key>:v<version>
  canonical_value
  owner_identity_id    # 逻辑实体，不是某一条历史记录版本
  source_record_id     # 最近一次产生/更新该 claim 的记录
  is_active
  created_at / released_at
```

唯一索引键（均只对 `is_active=true` 生效）：

| 唯一作用域 | 实际唯一键 | 语义 |
| --- | --- | --- |
| 项目全局 | `(project_id, field_key, "project", "*", canonical_value)` | 跨入口、跨模板版本、跨活动唯一；例如员工工号、资产编号 |
| 项目范围 | `(project_id, field_key, "scope", canonical_scope_key, canonical_value)` | 在配置声明的范围内唯一；范围 key 必须包含项目定义的稳定维度，不包含随机运行 ID |
| 模板版本 | `(project_id, field_key, "template", "template:<definition-key>:v<version>", canonical_value)` | 只在同一模板定义的同一发布版本内唯一；允许不同版本重新使用，但不能绕过项目全局规则 |

项目全局或范围唯一字段若跨模板版本继续表示同一业务字段，必须复用相同 `field_key`，因此仍由同一 claim 命名空间阻止重复。若业务含义变化，必须新建 field key 或执行显式数据迁移。

规范化规则：

- Unicode NFKC、去首尾空白、连续空白折叠；默认大小写不敏感，字段可声明大小写敏感。
- 数值使用十进制规范字符串，日期使用 ISO-8601；禁止把本地显示格式直接作为 key。
- 空值不产生 claim；显式占位符（如 `未知`）只有 Schema 明确允许时才进入唯一校验。
- `canonical_scope_key` 使用 Schema 声明的 scope 字段按稳定顺序拼接并转义分隔符，禁止使用显示名称或文件路径。

生命周期：

- 预检查不写永久 claim；如需防止同一页面重复点击，只使用带 TTL 的 `ImportReservation`，过期自动释放。
- `pending`/`rejected`/`abandoned`/`failed` 记录不占用永久 claim。
- `effective`/`adopted` 记录占用 claim；同一逻辑实体产生新 revision 时，旧 claim 在同一事务中改为 inactive，新 claim 指向同一 `owner_identity_id`。
- 软删除或撤回释放 active claim，但保留 inactive 历史行和审计；物理清理必须走保留期和备份门禁。
- 重提不是覆盖旧记录，而是新 `CollectionRun`/新 record revision；只有审核采用后才竞争 active claim。

并发和 SQLite：

1. 事务先锁定 `CollectionProject` 和涉及的 `RecordIdentity`，再读取/写入 claim。
2. 写入 active claim 使用 partial unique index；SQLite `IntegrityError` 转成确定性的“字段已存在”行错误，不返回 HTTP 500。
3. 所有事务保持短小，媒体写入在事务外先落盘并校验，数据库只在最终引用阶段写入；使用现有 busy timeout/重试策略。
4. claim 冲突回滚本次记录，不回滚同一运行中已经验证的其他不冲突记录；运行仍可进入人工修正或整批退回。

## D3：导出规则所有权和历史快照

规则分层：

- `TemplateVersion.field_export_json`：字段层规则。决定哪些稳定 field key 可以出现在明细/汇总/异常表、显示列标题、顺序、类型和脱敏方式。
- `ProjectConfigVersion.export_policy_json`：项目层规则。决定目录层级、分类字段、文件命名、重名后缀、附件目录和工作表名称。
- `CollectionRun.export_snapshot_json`：采集开始时由上述两者规范化生成的只读联合快照，包含模板/配置版本号和 SHA256。

因此：

- 修改字段类型、字段列映射、字段是否可导出，必须创建新的 `TemplateVersion`。
- 修改目录布局、命名、分类、附件策略或工作表名称，只创建新的 `ProjectConfigVersion`。
- 导出永远使用目标 `CollectionRun` 保存的 `export_snapshot_json`；不得按当前最新模板或配置重解释历史材料。
- 管理员若要使用新规则导出历史数据，必须显式创建一个“重投影导出”任务，记录新规则版本和原始运行版本，不能改变原运行的默认可追溯导出。
- 非学生项目的工作表名称、字段名和目录名完全来自模板/配置；不能强制使用“学生明细”。V1 学生项目继续输出 `统计汇总`、`学生明细`、`异常记录`。

## D4：活动、提交、记录、采用和补传状态机

### 对象定义

- `CollectionActivity`：一个项目入口在一个采集周期/范围内的业务活动，负责开放/暂停/关闭和采用策略。
- `CollectionRun`：一次完整上传/提交尝试，是活动下可重复创建的版本，不是活动本身。
- `CollectionRecord`：某次 run 中的一行数据版本；逻辑实体由 `RecordIdentity` 标识。
- `EffectiveVersion`：活动范围内当前生效 run/record 的指针。
- `SupplementRun`/`ChangeRun`：针对已生效版本的补传或变更请求，不能直接改原记录。

### 状态

```text
Activity:   draft -> open -> paused -> open -> closed -> archived
Run:        uploading -> finalizing -> submitted -> reviewing
                         -> adopted -> superseded
                         -> rejected / abandoned / expired
Record:     staged -> validated -> pending -> effective
                                      -> rejected / superseded
Supplement: uploading -> pending -> approved / rejected / expired
```

只允许服务端定义的状态转移；重复请求返回当前稳定结果。`adopted -> superseded` 和 `Supplement approved` 必须在锁定活动/范围后同一事务提交。

### 采用策略

`CollectionActivity.adoption_mode` 枚举：

| 场景 | adoption mode | 唯一生效指针 |
| --- | --- | --- |
| 学生整班替换 | `replace_scope` | `(activity_id, scope_key)` 只能一条 current `EffectiveVersion`；新 run 采用后旧 run superseded |
| 员工单记录更新 | `record_revision` | `(activity_id, identity_id)` 一条 current record；同一活动可有多个 run 更新不同员工 |
| 资产唯一编号 | `global_claim` | 由 D2 项目全局 claim 控制；冲突记录不能采用 |
| 活动报名 | `append` | 每个稳定报名 key 一条 effective record；不要求整批替换，撤销使用新审核动作 |

V1 adapter 把每个旧 `ClassGroup`/学年范围映射为一个 `replace_scope` 活动；`Submission(status="approved")` 映射为当前 `EffectiveVersion`，保持 V1 每班一条采用版本的约束。

### 补传

补传引用 `parent_run_id` 和目标 `RecordIdentity`，创建新的 `SupplementRun`，保留原始文件和绑定审计。审核时必须再次确认 parent run 仍是活动当前生效版本；若已被替代，补传转 `expired`，不能跨 run、跨项目或静默覆盖照片/资产。

## D5：安装升级、回滚和失败停机

### 升级时序

```text
预检查 -> 持久维护锁 upgrading -> 阻断新写入 -> 排空已有写入
  -> 独立路径整套备份+manifest/SHA256/CRC验证
  -> 停止应用服务 -> 迁移 dry-run -> 执行正向迁移
  -> 启动新版本 -> 健康检查/读写检查/媒体清单检查
  -> 成功则保持 gate=closed，管理员明确开启
```

升级前备份至少包含 SQLite online backup、`media/`、项目配置、模板版本、当前入口指针、安装配置和版本清单；备份写入异机或独立挂载路径，禁止覆盖已有备份。

SQLite 迁移默认只支持向前，不把旧应用指针回退称为数据库回滚：

- 新程序启动失败但数据库尚未迁移：可回退程序目录并保持 gate closed。
- 迁移已提交后新程序失败：禁止直接启动旧程序；必须用验证过的升级前整套备份恢复 SQLite、媒体和配置，再启动旧版本。
- 恢复失败或 manifest 不一致：进入 `recovery_required` 停机态，所有写入拒绝，保留现场和日志，不能自动删除原媒体。

平台差异：

- Windows：停止 Windows Service、确认子进程退出、锁文件句柄释放、恢复后再启动服务；失败退出码写入事件日志。
- Linux：`systemctl stop` 后确认 worker 已退出、systemd unit 不自动拉起恢复中的旧/新程序；恢复完成再启动。
- Docker：`docker compose stop web nginx`，恢复挂载目录后执行 `compose up`；不在容器启动命令中自动执行不可逆迁移。

统一退出码：`0` 成功；`10` 预检查失败且未改动；`20` 备份验证失败且未迁移；`30` 迁移失败但已完整恢复；`40` 恢复失败/进入停机待人工处理；`50` 健康检查失败且已恢复或保持停机。

## V1 Excel 兼容契约

- V1 内置模板主工作表标题在第 4 行，数据从第 5 行开始；前两行是说明，且可能有合并单元格。
- V1 `parse_roster()` 只在前 10 行搜索包含 `姓名`、`学部`、`学年`、`班级`、`就读类型` 的表头，因此第 4 行必须继续可识别。
- V1 旧模板缺少年级时，继续按旧学年映射补出年级；V2 新模板的年级字段不改变 V1 解析语义。
- V2 内置学生预设仍保持第 4 行表头和第 5 行示例数据；`模板元数据` 可放在额外工作表，不得把说明行插入第 4 行之前导致旧解析改变。
- 新项目使用 V2 入口和 V2 importer，不把新项目的元数据工作表强行交给 V1 `/submit` 解析器。

