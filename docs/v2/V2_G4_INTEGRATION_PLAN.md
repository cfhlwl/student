# Student Collector V2：G4 并行实施与集成验收计划

本轮只交付计划和设计，不创建实现分支、不合并、不部署、不写生产数据。实现阶段从已审查的 V1 主分支另开 worktree。

## 1. 阶段门禁

| 阶段 | 内容 | 进入条件 | 退出条件 |
| --- | --- | --- | --- |
| P0 | 数据契约、Schema、兼容 adapter、安装契约 | G0 设计复审通过 | 契约测试和迁移 dry-run 通过 |
| P1 | 模板引擎和通用采集链路 | P0 完成 | 预览、分批上传、匹配、审核、导出通过 |
| P2 | 配置中心和权限 | P0 Schema 稳定 | 多角色、版本发布/回滚、审计通过 |
| P3 | Windows/Linux/Docker 安装包 | P0 安装契约稳定 | clean VM 安装、升级、恢复通过 |
| P4 | E 线集成 | A-D 各自 Gate 通过 | 全平台矩阵、旧数据兼容、无阻断缺陷 |

## 2. 工作线和文件所有权

| 线 | 范围 | 主要文件所有权 | 前置依赖 | 交付 |
| --- | --- | --- | --- | --- |
| A：Schema/导入导出 | 通用模型、模板版本、Excel、动态校验、匹配和导出 adapter | `collector/v2/domain/`、`collector/v2/importer/`、`collector/v2/exporter/`、对应 migrations/tests | P0 契约；不修改 V1 services 语义 | Draft PR、迁移/Excel/ZIP 对账证据 |
| B：配置中心 | 项目、模板草稿/发布、配置版本、成员权限、审计页面 | `collector/v2/admin/`、`templates/v2_admin/`、`static/js/v2_admin/` | A 的 Schema API；使用独立 URL 命名空间 | Draft PR、角色/并发/回滚测试 |
| C：安装器 | Windows x64、Linux systemd、Docker/离线包、健康检查 | `installer/windows/`、`installer/linux/`、`deploy/v2/`、安装文档 | P0 安装契约；不改业务模型 | Draft PR、clean VM 和离线证据 |
| D：兼容/安全/数据迁移 | legacy adapter、迁移命令、CollectionEntry/Activity/Identity/EffectiveVersion、动态唯一值命名空间、备份格式兼容、跨平台锁、限流和安全 | `collector/v2/compat/`、`collector/management/commands/`、`collector/locking.py`、security tests | A 的模型契约；不删除 V1 表 | Draft PR、旧库 dry-run/恢复/安全证据 |
| E：集成 | 只解决接口、迁移和模板冲突，维护集成报告 | `docs/v2/`、集成测试和报告 | A-D Gate 全部完成 | 独立 integration commit、完整回归报告 |

规则：同一文件只由一条线修改；共享接口先写契约测试；跨线变更通过 E 线 cherry-pick/整合，不覆盖另一条线提交历史。

## 2.1 D1-D5 共享契约

集成阶段以 `V2_D1_D5_CONTRACTS.md` 为唯一解释来源，任何实现、迁移、测试或安装脚本不得重新解释以下规则：

- D1：一个项目可以同时启用多个 `CollectionEntry`；每个入口只有一个当前发布模板版本，项目只有一个当前发布配置版本指针。新会话必须通过项目/入口 URL 解析并绑定入口、模板、配置快照；V1 `/submit` 固定映射内置学生入口。入口切换只影响新会话，旧会话、结果链接和历史导出继续使用创建时快照。
- D2：有效唯一声明使用 `(project_id, field_key, namespace_kind, namespace_key, canonical_value)` 的部分唯一索引；项目全局、作用域、模板版本分别使用明确命名空间。空值不占用声明，拒绝/废弃/失败记录不永久占用，历史有效记录保留但只能有一个 active owner；并发冲突转为确定性校验错误，不以 SQLite 长事务解决。
- D3：字段列映射和字段级导出投影归 `TemplateVersion`；目录、命名、附件、工作表名称和分类归 `ProjectConfigVersion`；联合规范化快照归 `CollectionRun.export_snapshot_json`。历史导出只读运行快照，不能被当前配置改写；V1 的三个工作表名称保持兼容。
- D4：`CollectionActivity` 是采集活动范围，`CollectionRun` 是一次上传/提交版本，`CollectionRecord` 是记录，`EffectiveVersion` 是当前采用版本，`SupplementRun` 是补传。状态机和采用模式固定覆盖学生班级替换、员工记录修订、资产全局唯一、活动报名追加四类业务；补传必须引用父版本并在父版本失效时进入过期/待审状态。
- D5：升级或清空先取得持久维护锁并排空写入，再做不可覆盖的 SQLite、媒体、配置和版本清单全量备份及校验；数据库迁移提交后不能用旧程序指针回退，失败只能停止服务并恢复完整备份，恢复失败进入 `recovery_required`。成功升级仍保持上传关闭，必须由管理员显式重新开启。

## 3. 必须覆盖的测试矩阵

### 业务和兼容

- V1 内置学生项目：旧模板、无年级旧模板、2024/2025/2026 年级映射、走读/半走读/住宿。
- 新项目：文本、整数、日期、下拉、布尔、照片键、附件字段。
- 同一模板多个版本、发布后锁定、历史记录按旧版本读取。
- 50/100/300 张照片分批，网络中断、重复请求、刷新恢复、补传审核。
- 多入口同时启用、入口切换、入口停用、旧会话/结果链接继续按原绑定访问，新会话只进入当前发布版本。
- 学生班级替换、员工记录修订、资产唯一 ID、活动报名追加四种采用模式；父版本失效后的补传不得跨版本采用。

### 数据一致性

- 数据库记录数量、私有磁盘文件数量、原始字节 SHA256、资产 manifest 一致。
- 同名学生、重复唯一值、重复文件名、跨项目/跨班级绑定均阻断或进入人工确认。
- 项目全局唯一、作用域唯一、模板版本唯一的跨版本测试；空值、拒绝后重提、历史保留、同一唯一值并发竞争和 SQLite 两 worker 冲突测试。
- 单班、年级、学部、全校导出；ZIP CRC、照片字节、Excel 数值单元格、异常记录一致。
- 分别修改模板字段投影和项目导出策略，验证历史运行的导出快照、目录/命名/工作表名称不被当前配置改写。
- 旧 V1 备份验证/恢复；V2 备份验证/恢复；失败注入后数据库和媒体均可回滚。

### 安全和并发

- 角色越权、跨项目访问、错误凭证、CSRF、路径穿越、Excel 公式注入。
- 两个 Gunicorn worker、两个 Windows service process 同时上传/审核/备份/清空。
- 可信代理 XFF 伪造、共享出口 IP 限流、磁盘配额临界、临时文件清理。
- 配置发布与采集会话并发；清空与上传并发；维护锁失败不返回 500。

### 平台

- Windows 11 x64、Windows Server x64、Linux x64、Docker Compose。
- 安装、启动、停止、重启、升级、失败回滚、卸载保留数据、离线安装。
- Windows/Linux/Docker 升级中断和失败注入：确认写入排空、备份清单 SHA/CRC、恢复后数据库/媒体/配置一致、服务停机状态和退出码 10/20/30/40/50；成功升级后上传仍关闭。
- 同一虚构数据在三种交付形态导出结果对账。

### V1 Excel 表头兼容性

- 使用真实 V1 学生模板验证：主表表头在第 4 行、示例数据在第 5 行；解析器仍只在前 10 行寻找表头。
- V2 内置学生预设继续输出上述 V1 行号和列名；V2 扩展元数据放在额外工作表，不改变 V1 主表。
- V1 `/submit` 和旧数据导入不切换到通用项目导入器；新项目单独使用 V2 导入器，防止列映射和历史学年语义回归。

## 4. 每条 Draft PR 的证据格式

```ini
BASE_SHA=
EXACT_HEAD=
TREE=
CHANGED_FILES=
LOCAL_TEST_EXIT=
WINDOWS_TEST_EXIT=
LINUX_TEST_EXIT=
DOCKER_TEST_EXIT=
DATA_SHA256_GATE=
ZIP_EXCEL_GATE=
SECURITY_GATE=
BLOCKERS=
MERGE=HOLD_FOR_CHATGPT
DEPLOY=NO
```

不能用 HTTP 200 代替数据内容对账；不能用 GitHub Actions 代替本地可复现证据。每条线必须说明未覆盖范围和测试数据清理结果。

## 5. 集成顺序

1. E 先冻结 `V2_G0` 中的模型、Schema、版本和 storage key 契约。
2. 合入 A，执行 D1-D4 契约测试、迁移 dry-run、内置预设、旧模板和 ZIP 对账。
3. 合入 D，执行 legacy adapter、备份/恢复、锁和安全回归。
4. 合入 B，执行权限、配置发布/回滚、移动端后台回归。
5. 合入 C，执行 D5 clean VM、离线包、升级回滚和 Docker 回归。
6. 运行全量集成矩阵；任何数据库、文件哈希、ZIP/Excel 统计或跨项目隔离差异都阻断集成。

## 6. 本轮阻塞项

- 尚未决定 Windows 原生包的签名证书、服务包装器许可和正式分发渠道。
- 尚未决定新项目是否在 V2 首期允许把 legacy 学生项目切换到通用写链路；设计默认不切换。
- 尚未提供学校统一全校应采名册，因此覆盖率只能报告“已提交名单内完成率”，不能冒充全校覆盖率。
- Windows Server、Linux clean VM 和离线介质尚未实测；在实现阶段补齐后才能进入部署评审。

