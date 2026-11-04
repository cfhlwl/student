# R3 集成契约（基于 R3-A）

本文件仅供 E 线集成使用，专项线不得修改其他线所有权文件。

## 数据口径

- 统计和导出只读取 `Submission.status="approved"` 且为同一 `ClassGroup` 当前有效版本的数据。
- 学年使用 `SchoolYear.name`；年级使用 `ClassGroup.grade`，不能从学年推导年级。
- 照片状态使用现有 `Student.match_status`、`StudentPhoto` 和 `SupplementPhoto.match_status` 契约。

## A 线预检查

建议服务接口：`preflight(roster_upload, photo_uploads, document_uploads=[]) -> dict`。
返回 `student_count`、`matched_count`、`missing_count`、`manual_count`、`errors`、`warnings`、`can_submit`，不得创建 `UploadSession` 或写入持久化上传文件。

## B 线统计

建议服务接口：`collection_statistics(filters) -> dict/list`，异常工作簿生成函数应返回临时文件路径并由调用方负责响应关闭后的清理。工作簿不得包含未过滤的其他班级学生信息。

## C 线 UI

- 结果页保留 `submission.id` 和 `token` 的现有私有访问契约。
- 管理员页面继续使用现有命名 URL；危险清空按钮必须保留并增强确认提示。
- 不把学生资料或上传令牌写入公共静态资源、日志或页面外部链接。

## D 线安全/备份

- 可信代理策略必须集中在安全模块或中间件，普通请求不能直接信任任意 `X-Forwarded-For`。
- 限流不得依赖单进程内存状态绕过多 Gunicorn worker；若 SQLite 不适合共享计数，应采用文件/数据库锁或明确的部署级方案。
- 备份必须同时覆盖 SQLite 与 `MEDIA_ROOT`，并输出逐文件 SHA256 清单；恢复脚本必须拒绝校验失败的备份。

E 线负责将专项模块接入 `collector/urls.py`、`collector/views.py`、模板上下文和迁移，处理冲突并执行最终联验。
