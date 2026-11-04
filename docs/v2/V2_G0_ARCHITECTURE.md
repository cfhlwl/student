# Student Collector V2：G0 现状审计与目标架构

审计基线：`a748678d8c9b56d1d848db5a43f00accc4295406`

本文件是设计阶段交付物，不包含数据库迁移、运行时 DDL、生产数据操作或部署变更。

## 1. V1 现状审计

| 范围 | 当前实现 | V2 结论 |
| --- | --- | --- |
| 业务模型 | `SchoolYear`、`ClassGroup`、`Submission`、`Student`、`StudentPhoto`、`SubmissionDocument` | 保留为 legacy 兼容边界；新增通用项目模型，不直接把固定字段改成动态列 |
| Excel | `collector.services.HEADERS` 固定列；`parse_roster()` 固定必填列、枚举和旧学年年级映射；`template_bytes()` 生成一个固定模板 | 抽象为不可变模板版本和字段 Schema；内置“学生资料采集”预设继续输出旧模板兼容格式 |
| 上传 | `UploadSession`、`UploadBatch`、`StagedUpload`；批次有清单、顺序、SHA256 和幂等校验 | 复用上传可靠性契约；会话必须固定项目和模板版本 |
| 匹配 | 默认按唯一姓名文件名匹配；同名进入人工确认；补传受提交凭证保护 | 把匹配键、冲突策略和人工确认策略放入模板版本配置 |
| 审核 | 提交版本按班级唯一采用；补传审核会检查原版本有效性 | 兼容已有审核状态；通用项目使用版本化记录和审核状态 |
| 导出 | 只读已采用数据；按学年/学部/年级/班级/就读类型分层；ZIP 内含 Excel、照片、文档并做完整性校验 | 抽象为导出规则快照；旧学生导出路径保持不变 |
| 管理配置 | `SchoolYear`、`ClassGroup`、`NamingRule`、`AppSetting` | 新增项目级配置版本；旧设置通过 legacy adapter 映射 |
| 维护与备份 | 文件锁 + 状态文件；Linux 使用 `fcntl`，Windows 当前仅进程内回退锁；SQLite online backup + 媒体清单 | V2 必须提供跨进程、跨平台锁，并保留 V1 备份格式读取能力 |
| 部署 | Docker Compose、Gunicorn、Nginx；私有 `data/` 和 `media/` | 增加 Windows x64 原生服务、Linux systemd 和离线包；Docker 仍为兼容安装方式 |

关键代码证据：

- 固定列和旧学年兼容映射位于 `collector/services.py` 的 `HEADERS`、`FIELD_MAP`、`LEGACY_GRADE_BY_YEAR`。
- 固定业务字段及唯一采用约束位于 `collector/models.py` 的 `ClassGroup`、`Submission`、`Student`。
- 上传可靠性边界位于 `UploadSession`/`UploadBatch`/`StagedUpload` 及 `stage_upload_batch()`、`finalize_upload_session()`。
- 导出和原始附件完整性校验位于 `export_students_to_tempfile()`、`_stage_export_file()`。
- 当前 Windows 维护锁回退为线程锁，不能作为多进程 Windows 服务的最终实现。

## 2. 目标架构

V2 仍是一个 Django 单体应用、SQLite 单实例、本地私有文件存储。通用化通过 Schema 和配置数据完成，不通过运行时新增表字段完成。

```text
安装层
  Windows Service / Linux systemd / Docker Compose
        |
HTTP 层
  Django Templates + Bootstrap + 上传会话 API
        |
平台层
  项目、模板版本、配置版本、权限、审计、备份恢复
        |
采集层
  Schema 校验 -> Excel 导入预览 -> 分批上传 -> 匹配 -> 审核 -> 导出
        |
持久化层
  SQLite 结构化表 + JSON Schema 数据 + 私有媒体 + SHA256 清单
```

### 2.1 通用数据模型

新增模型采用 Django 正常迁移，所有动态内容放在受校验的 JSON 中：

| 模型 | 主要字段 | 约束 |
| --- | --- | --- |
| `CollectionProject` | `id`、`slug`、`name`、`status`、`current_template_version`、`active_config_version`、`created_at` | `slug` 唯一；删除采用停用，不级联删除历史数据 |
| `TemplateDefinition` | `project`、`key`、`name`、`preset`、`created_at` | 一个项目可有多个模板；内置预设不可删除 |
| `TemplateVersion` | `template`、`version`、`state`、`schema_json`、`mapping_json`、`matching_json`、`export_json`、`schema_sha256`、`published_at` | `(template, version)` 唯一；发布后不可修改 |
| `ProjectConfigVersion` | `project`、`version`、`state`、`settings_json`、`settings_sha256`、`created_by` | 发布后不可修改；项目只能有一个当前版本 |
| `CollectionRun` | `project`、`template_version`、`config_version`、`scope_json`、`status`、`access_token_hash` | 采集时锁定模板和配置快照；历史记录不可被新配置重解释 |
| `CollectionRecord` | `run`、`external_key`、`values_json`、`normalized_json`、`status` | 项目内按模板定义的唯一键校验；保留原始单元格错误 |
| `RecordUniqueValue` | `project`、`template_version`、`field_key`、`scope_key`、`canonical_value`、`record` | 唯一索引承接动态字段唯一性，避免依赖 SQLite JSON 查询约束 |
| `CollectionAsset` | `project`、`run`、`record`、`kind`、`original_name`、`storage_name`、`mime`、`size`、`sha256` | 文件路径只由服务端生成；记录原始字节哈希和导入批次 |
| `ReviewAction` | `run`/`record`、`action`、`actor`、`before_json`、`after_json`、`created_at` | 审核和人工匹配不可覆盖历史操作 |
| `ProjectMember` | `project`、`user`、`role` | V2 先提供 `owner`、`editor`、`reviewer`、`exporter`、`viewer` 五级最小权限 |

现有 `Submission`、`Student`、`StudentPhoto` 等表不删除、不重命名、不改语义。V2 首期使用 `LegacyStudentProjectAdapter` 把它们映射为内置项目的只读/兼容视图；待新项目链路稳定后，再通过显式迁移命令选择性迁移，不在启动时自动搬运生产数据。

### 2.2 Schema 约束

`TemplateVersion.schema_json` 只允许白名单字段：

- `key`：稳定机器标识，发布后不可更改；
- `label`：Excel 显示名称；
- `type`：`text`、`integer`、`decimal`、`date`、`choice`、`boolean`、`image_key`、`attachment`；
- `required`、`unique`、`max_length`、`pattern`、`choices`、`order`；
- `excel`：列顺序、列宽、示例、说明；
- `matching`：可作为照片/附件匹配键的字段及冲突策略；
- `export`：是否进入统计、明细、异常工作表和文件名模板。

Schema 通过 JSON Schema 风格的服务端校验器检查。未知字段、非法表达式、路径片段、任意 Python 代码和任意 SQL 均拒绝保存。任何字段增加/删除都只产生新版本，不改变旧记录。

## 3. 迁移方案

采用 expand -> backfill -> verify -> switch，禁止运行时 DDL：

1. 增加通用表和 nullable 关联字段；不改 V1 现有列含义。
2. 创建内置项目及“学生资料采集”模板草稿，读取旧 `HEADERS` 和旧枚举生成 Schema。
3. 只读校验旧数据，生成对账报告：提交数、学生数、照片数、附件数、采用版本数、哈希差异。
4. 显式命令 `migrate_legacy_collection --dry-run` 生成候选映射；管理员确认后才写入通用表。
5. 双读验证：V1 `/submit`、V1 `/admin` 和旧导出继续读旧表；V2 项目页可读 adapter 数据。
6. 新项目只写通用表；内置学生项目在兼容期仍由旧入口写旧表，避免线上行为突然改变。
7. 经过完整回归后，才允许在单独发布阶段切换内置项目的写入口；切换可回滚，旧表保留。

SQLite 注意事项：迁移脚本必须短事务、可重入、可中断恢复；大媒体不进入数据库；所有 backfill 分页并记录 checkpoint；失败时不删除旧表和原文件。

## 4. 风险矩阵

| 风险 | 影响 | 缓解 | 阻断门禁 |
| --- | --- | --- | --- |
| 动态字段查询难以建立数据库约束 | 重复记录、统计不准 | `RecordUniqueValue` 规范化唯一索引 + 服务端二次校验 | 唯一性、并发重复导入测试 |
| 旧学生数据与新 Schema 语义不一致 | 历史导出变化 | legacy adapter、模板版本快照、旧入口不切换 | 旧数据 ZIP/Excel 字节和数量对账 |
| 模板发布后被修改 | 历史数据不可追溯 | 发布版本不可变，配置引用版本号和 SHA256 | 版本锁定测试 |
| JSON 字段被注入公式或路径 | Excel/文件安全问题 | Excel 值转义、文件名白名单、禁止模板代码执行 | 恶意字段/文件名测试 |
| Windows 多进程维护锁不生效 | 清空/备份期间数据损坏 | `portalocker` 或等价 OS 锁；跨进程联测 | 两个服务进程锁竞争测试 |
| SQLite 并发和迁移长事务 | HTTP 500/锁等待 | 短事务、重试、迁移前备份、单实例明确边界 | 10/20 并发真实 HTTP 测试 |
| 原生安装缺依赖或升级失败 | 无法启动/数据丢失 | 离线 wheelhouse、健康检查、备份后原子切换、回滚 | Windows Server/Linux clean VM 测试 |
| 跨平台路径/权限差异 | 文件无法读写或越权 | 统一 storage key，`pathlib`，服务账户最小权限 | Windows/Linux 文件存储矩阵 |

## 5. G0 通过标准

- 设计不需要运行时 DDL；动态字段都由已校验 Schema + JSON/唯一值表承载。
- V1 现有入口、模型、导出和备份格式有明确兼容边界。
- 所有迁移都可 dry-run、可对账、可回滚，且不自动改生产资料。
- Windows/Linux/Docker 的锁、路径、服务、备份差异已进入后续测试矩阵。

