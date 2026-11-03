# V1 测试及自审报告

日期：2026-11-03（Asia/Shanghai）

## 当前 Git 证据

- 项目目录：`E:\cfyz\student-collector-v1`
- Git SHA：以交付时 `git rev-parse HEAD` 输出为准（本报告不自引用提交哈希）
- GitHub Actions：未启动
- 父目录 `cfyz`、EasyPrinter 和现有生产数据库/服务：未访问、未修改

## 已执行命令与退出码

| 检查 | 命令 | 结果 |
|---|---|---:|
| Django 系统检查 | `python manage.py check` | 0 |
| 迁移漂移检查 | `python manage.py makemigrations --check --dry-run` | 0 |
| 自动化回归 | `python -m pytest -q` | 0，9 passed |
| Compose 配置 | `docker compose config` | 0 |
| Docker 镜像构建 | `docker build -t student-collector-v1:local .` | 0 |
| 容器空库迁移 | `docker run --rm ... python manage.py migrate --noinput` | 0 |
| HTTP 页面冒烟 | `/submit`、模板下载、`/admin/login/` | 200 |
| 未登录后台边界 | `/admin/` | 302 到登录页 |
| 已登录后台页面 | 概览、提交版本、学生资料、命名、班级 | 200 |

## 自动化覆盖

- 显式照片文件名匹配和私有文件落盘。
- Excel 重复记录和跨班级提交拒绝。
- 同名学生不随机匹配，进入待人工确认列表。
- 管理员人工匹配只允许绑定同一提交版本，且不覆盖已有照片。
- 多班级导出：每个班级有 `学生统计表.xlsx` / `学生照片/`，跨班级有 `总统计表.xlsx`。
- 导出统计工作表包含统计汇总、学生明细、异常记录；最终文件名可追溯。
- 公式样式输入在导出 Excel 中加前缀，避免公式注入。
- 一次性提交凭证、跨班级访问边界、管理员登录边界、关闭公开上传。
- 示例 Excel 模板可读取，虚构测试数据可生成。

## 自审结论

已完成 V1 核心代码自审，未发现会阻止本地运行的明显缺陷。原始上传文件按提交 UUID 分目录，导出文件不写入公共静态目录；默认导出只查询管理员“已采用”版本；管理操作写入 `AuditLog`。

以下事项不是本地隔离开发阶段的假设性“已通过”，而是正式发布前的门禁：

- 生产 HTTPS 证书、域名、Nginx TLS 配置、真实反向代理头和安全扫描。
- 学校授权/保留期限/备份保留期确认，以及真实数据脱敏联验。
- SQLite 单实例在学校实际网络下的并发提交、断网重试和恢复演练。
- 备份文件跨机器恢复演练，确认数据库和 `media/` 配套恢复。
- 根据实际班级规模校准 Nginx、单文件大小、数量和 IP 限流参数。
- 上线前人工检查所有导出筛选组合、半走读独立分类和重名后缀。

