# R3-B B 线：管理员采集管理与异常统计

## 范围

本专项只新增统计计算、管理员统计模板和隔离测试，没有修改模型、迁移、现有视图、URL、公共模板或 ZIP 导出核心。

统计口径：

- 每个学年 / 学部 / 年级 / 班级只取当前有效的 `Submission(status="approved")`。
- 如果历史数据意外存在多个已采用版本，按 `submitted_at` 和提交编号取最新版本，旧版本不计入学生数、照片数或异常名单。
- 存在待审核替代提交时，统计仍显示当前有效采用版本，并单独报告待审核提交数量。
- 名单内照片完成率 = 当前有效采用版本中有数据库照片记录且学生状态为“已匹配”的学生数 / 当前有效采用版本学生数。
- 全校覆盖率不使用“已提交人数”冒充。只有配置 `AppSetting(key="school_expected_student_count")` 为正整数时才计算，否则明确返回不可计算及原因。

## 接口契约（由 E 线接入受保护管理员视图）

```python
from collector.statistics import (
    build_admin_statistics_context,
    build_exception_workbook,
    build_exception_rows,
)

context = build_admin_statistics_context(request.GET)
xlsx_bytes = build_exception_workbook(request.GET)
```

- `build_admin_statistics_context(filters)` 支持 `school_year`、`department`、`grade`、`class_name`、`status` 五个白名单筛选字段。
- `context["rows"]` 是每个采集范围一行，包含 `student_count`、`matched_photo_count`、`missing_photo_count`、`pending_exception_count`、`photo_completion_rate`、`pending_submission_count` 等字段。
- `context["coverage"]` 明确提供 `available`、`expected_count`、`covered_count`、`rate`、`reason`，避免把名单内完成率误称为全校覆盖率。
- `build_exception_workbook(filters)` 返回内存中的 XLSX 字节，工作表名为“异常名单”。
- `build_exception_rows(filters)` 返回同一异常明细的规范化二维数组，便于接口测试。
- 模板路径为 `collector/admin_statistics.html`，预期上下文为上述 `context`，另可传入 `exception_export_url` 接入下载 URL。

## 异常导出内容

异常表列为：异常类型、学年、学部、年级、班级、学生编号、姓名、性别、就读类型、照片文件名、异常原因。覆盖缺少照片、同名学生、匹配失败、待人工确认和待审核补传；同一异常键只写一次，并始终从当前有效采用版本取数。

所有用户可控文本经过 `safe_excel_value`，以单引号保护 `=`, `+`, `-`, `@` 等公式前缀；统计计数和完成率使用数值值写入。

## 测试证据

先在隔离 Docker 镜像中运行专项 RED：

```text
docker run --rm r3b-admin-red pytest -q tests/test_admin_statistics.py
exit=1
ModuleNotFoundError: No module named 'collector.statistics'
```

实现后专项 GREEN：

```text
docker run --rm r3b-admin-green pytest -q tests/test_admin_statistics.py
4 passed in 1.47s
exit=0
```

完整本地回归：

```text
docker run --rm r3b-admin-green pytest -q
68 passed in 17.13s
exit=0

docker run --rm r3b-admin-green python -m compileall -q collector config tests
exit=0

docker run --rm r3b-admin-green python manage.py check
System check identified no issues (0 silenced).
exit=0

docker run --rm r3b-admin-green python manage.py makemigrations --check --dry-run
No changes detected
exit=0
```

本线未创建 Draft PR 以外的集成接线，未修改其他工作树、未触发 GitHub Actions、未合并、未部署。
