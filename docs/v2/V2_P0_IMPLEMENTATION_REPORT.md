# Student Collector V2：P0 共享基础实现报告

## 范围

- 基线：`4ba44e83b5cea93318440c70f9f9df7a0339bfa8`
- Worktree：`E:\cfyz\student-collector-v2-p0`
- 分支：`codex/v2-p0-contracts`
- 目标：冻结通用模型、迁移、Schema/唯一 claim、导出快照、状态转换和跨平台维护锁接口。
- V1 生产表、V1 入口语义和现有部署未修改；没有生产数据库、媒体或服务写入。

## 已实现

1. `collector/models.py` 新增 V2 表：项目、入口、模板/模板版本、项目配置版本、采集活动、提交、记录、逻辑实体、采用版本、动态唯一值、资产、审核操作、项目成员和导入预占。
2. `collector/migrations/0014_*.py` 只创建 V2 表和索引；未删除、重命名或修改 V1 表。
3. `collector/v2_contracts.py` 固化 Schema 白名单、规范化唯一值、项目/范围/模板命名空间、active partial unique claim、状态转换和导出联合快照。
4. `collector/maintenance.py` 使用 `portalocker` 的跨进程共享/排他锁，Linux 与 Windows 共用同一接口；锁竞争返回 `MaintenanceError`。
5. `requirements.txt` 增加锁定依赖 `portalocker==3.2.0`。

## 本地验证

| 检查 | 结果 |
| --- | --- |
| `python manage.py check` | PASS，exit 0 |
| `python manage.py makemigrations --check --dry-run` | PASS，exit 0 |
| `python manage.py migrate --noinput` | PASS，exit 0；包含 `collector.0014` |
| P0 定向测试 | PASS，6 passed，exit 0 |
| 全量 pytest | PASS，102 passed，exit 0 |
| `git diff --check` | PASS，exit 0 |
| 迁移危险操作扫描 | PASS；未发现 `DeleteModel`、`RemoveField`、`RenameField`、`AlterField` |
| 跨进程维护锁 | PASS；独立子进程持有排他锁时，写入门禁按超时拒绝 |
| SQLite 唯一 claim 并发 | PASS；两线程结果为 1 success + 1 deterministic conflict，无 HTTP 500 异常泄漏 |
| Docker image build | PASS，exit 0；`student-collector-p0:local` |
| Docker `manage.py check` | PASS，exit 0 |
| Docker `manage.py migrate --noinput` | PASS，exit 0；包含 `collector.0014` |
| Docker pytest | PASS，102 passed，exit 0 |

## 尚未执行

```ini
DOCKER_GATE=PASS_LOCAL
WINDOWS_GATE=NOT_RUN
LINUX_GATE=NOT_RUN
V1_LEGACY_GATE=PASS_SCOPED_LOCAL
```

Docker、Windows clean VM、Linux clean VM 和真实安装产物属于 C/E 线门禁，本 P0 分支尚未冒充通过。P0 仅在隔离本地 SQLite 上验证；V1 兼容回归由现有 102 项全量测试覆盖，正式跨平台/备份恢复验收留给后续线。

## 交付门禁

```ini
SCHEMA_GATE=PASS
MIGRATION_GATE=PASS
SQLITE_CONCURRENCY_GATE=PASS
MAINTENANCE_LOCK_GATE=PASS_LOCAL
P0_GATE=READY_FOR_REVIEW
MERGE=HOLD_FOR_CHATGPT
DEPLOY=NO
GITHUB_ACTIONS=NO
```
