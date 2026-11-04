# V2 设计阶段自审记录

## 基线

- 仓库：`cfhlwl/student`
- 基线：`a748678d8c9b56d1d848db5a43f00accc4295406`
- Worktree：`E:\cfyz\student-collector-v2-design`
- 分支：`codex/v2-design-plan`
- 生产写入：无
- Docker/Nginx/正式服务器：未操作
- GitHub Actions：未触发

## 已完成审计

- 固定 Student/Submission/ClassGroup 模型、固定 Excel 列契约和旧学年映射。
- UploadSession/UploadBatch/StagedUpload 的分批、清单、SHA256、幂等和完成状态。
- 姓名匹配、同名人工确认、补传审核、唯一采用版本和分类导出。
- SQLite、跨 worker 维护锁、备份恢复、私有媒体和当前 Docker Compose。
- 当前 Windows 维护锁仅有线程级回退这一跨平台风险。
- PR #9 审查提出的 D1-D5 已集中写入 `V2_D1_D5_CONTRACTS.md`，并回填 G0-G4：多入口/模板指针、动态唯一声明、导出规则所有权和快照、活动/提交/采用/补传状态机、跨平台升级恢复时序。
- V1 Excel 兼容契约锁定为主表第 4 行表头、第 5 行数据、前 10 行表头探测；V2 内置学生预设和 V1 `/submit` 不改用通用导入器。

## 本地证据

| 检查 | 退出码 | 结果 |
| --- | ---: | --- |
| `git diff --check` | 0 | 通过；设计文档无空白/补丁格式错误 |
| `python manage.py check` | 1 | 阻塞：当前工作机未安装 Django，报 `ModuleNotFoundError: No module named 'django'` |
| `python manage.py makemigrations --check --dry-run` | 未执行 | 被上一项依赖错误阻断 |
| `python -m pytest -q` | 未执行 | 被上一项依赖错误阻断 |

本轮没有用未执行的测试结果冒充通过。实现阶段应在隔离 Docker 或安装 `requirements.txt` 后重新执行 Django check、迁移检查、pytest、Windows/Linux clean VM 和 Docker/Nginx 联验。

## 自审结论

- 本轮仅新增/修改 G0-G4 设计文档，没有改变业务代码、迁移、模板、部署文件或生产数据。
- 设计明确禁止运行时 DDL，采用受校验 Schema、JSON 数据和动态唯一值表。
- V1 旧表和入口保留，通用项目通过 adapter 和显式迁移命令逐步接入。
- G0-G4 已统一引用 D1-D5 中心契约；唯一索引、状态机、快照所有权和安装回滚退出码均有对应实施验收项。
- `ARCHITECTURE_GATE=WAIT_FINAL_REVIEW`。

