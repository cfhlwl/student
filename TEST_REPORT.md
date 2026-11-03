# V1 测试及自审报告

日期：2026-11-03（Asia/Shanghai）

## 当前 Git 证据

- 项目目录：`E:\cfyz\student-collector-v1`
- Git SHA：以交付时 `git rev-parse HEAD` 输出为准（本报告不自引用提交哈希）
- GitHub Actions：未启动
- 父目录 `cfyz`、EasyPrinter 和现有生产数据库/服务：未访问、未修改

## R1 RED 复现

- 旧实现使用单个 `/submit` multipart 请求提交 Excel + 105 张照片；Django 默认文件数量上限触发 HTTP 400。临时复现测试观察到 `1 passed`、退出码 0，断言内容为 `response.status_code == 400`，该测试随后删除，不作为 GREEN 测试保留。
- 旧实现的 `create_submission` 将文件校验、文件落盘、学生逐条写库都放在一个事务内；旧实现导出使用 `BytesIO` 和 `source.read()`，缺失照片直接抛出异常。代码审查与故障测试分别复现了这些路径。

## 已执行命令与退出码

| 检查 | 命令 | 结果 |
|---|---|---:|
| Django 系统检查 | `python manage.py check` | 0 |
| 迁移漂移检查 | `python manage.py makemigrations --check --dry-run` | 0 |
| 自动化回归 | `docker compose run --rm -v "${PWD}:/app" web pytest -q` | 0，29 passed |
| Django 系统检查 | `docker compose run --rm -v "${PWD}:/app" web python manage.py check` | 0 |
| 迁移漂移检查 | `docker compose run --rm -v "${PWD}:/app" web python manage.py makemigrations --check --dry-run` | 0，无变化 |
| Compose 配置 | `docker compose config` | 0 |
| Docker 镜像构建 | `docker compose build web` | 0 |
| R1 可靠性测试 | `docker compose run --rm -v "${PWD}:/app" web pytest -q -s tests/test_reliability_r1.py` | 0，11 passed；300 张导出 1.205 秒，tracemalloc 峰值 3,586,195 bytes |
| Docker + Nginx 真 HTTP | `R1_BASE_URL=http://nginx R1_ADMIN_PASSWORD=*** python tests/http_r1.py` | 0，`HTTP_R1_OK photos=50` |
| Compose 配置 | `docker compose config` | 0 |
| Docker 镜像构建 | `docker build -t student-collector-v1:local .` | 0 |
| 容器空库迁移 | `docker run --rm ... python manage.py migrate --noinput` | 0 |
| HTTP 页面冒烟 | `/submit`、模板下载、`/admin/login/` | 200 |
| 未登录后台边界 | `/admin/` | 302 到登录页 |
| 已登录后台页面 | 概览、提交版本、学生资料、命名、班级 | 200 |

## 自动化覆盖

- 唯一姓名照片匹配和原始学生名单私有文件落盘。
- 2024/2025/2026 学年与初高中年级自动推导。
- Excel 重复记录和跨班级提交拒绝。
- 同名学生不随机匹配，进入待人工确认列表。
- 管理员人工匹配只允许绑定同一提交版本，且不覆盖已有照片。
- 多班级导出：每个班级有 `学生统计表.xlsx` / `学生照片/`，跨班级有 `总统计表.xlsx`。
- 全部类别导出按走读、半走读、住宿分目录，并保留班级原始学生名单。
- 导出统计工作表包含统计汇总、学生明细、异常记录；最终文件名可追溯。
- 公式样式输入在导出 Excel 中加前缀，避免公式注入。
- 一次性提交凭证、跨班级访问边界、管理员登录边界、关闭公开上传。
- 示例 Excel 模板可读取，虚构测试数据可生成。
- 50、100、300 张照片分批上传；每批不超过 20 张且总大小不超过 32 MB。
- 上传后数据库照片数、私有磁盘文件数和 StudentPhoto.sha256 一致。
- 10 个不同班级并发提交，每班 20 张以内的隔离并发压力验证；无丢失、无串班、无重复提交。
- 批次重复重试不会重复写入；失败后状态可继续完成。
- 同班两个版本并发采用后数据库最多一个 `approved`，数据库部分唯一索引和事务审核共同保护。
- 单张照片物理缺失时 ZIP 仍可生成，统计 Excel 的异常记录包含文件缺失，照片明细不虚报成功导出。
- 导出使用受控临时文件和流式 `FileResponse`；响应关闭后临时 ZIP 删除。
- Excel 统计汇总的人数和照片数量为数值单元格。
- Docker + Nginx HTTP 联验实际完成 50 张分批上传、管理员审核、ZIP 下载、照片数量核对和统计 Excel 读取。

## 自审结论

已完成 V1 核心代码自审，未发现会阻止本地运行的明显缺陷。原始上传文件按提交 UUID 分目录，导出文件不写入公共静态目录；默认导出只查询管理员“已采用”版本；管理操作写入 `AuditLog`。

以下事项不是本地隔离开发阶段的假设性“已通过”，而是正式发布前的门禁：

- 生产 HTTPS 证书、域名、Nginx TLS 配置、真实反向代理头和安全扫描。
- 学校授权/保留期限/备份保留期确认，以及真实数据脱敏联验。
- SQLite 单实例在学校实际网络下的长时间并发提交、浏览器关闭后的重新选文件恢复演练。
- 备份文件跨机器恢复演练，确认数据库和 `media/` 配套恢复。
- 根据实际班级规模校准 Nginx、单文件大小、数量和 IP 限流参数。
- 上线前人工检查所有导出筛选组合、半走读独立分类和重名后缀。

## R1 修复摘要

1. 新增 `UploadSession`、`UploadBatch`、`StagedUpload`，上传先落私有临时目录，再短事务写入批次元数据；完成时统一生成独立提交版本。
2. 文件校验与落盘移出学生大事务；SQLite 锁等待使用有限重试，审核按班级事务互斥，并增加同班唯一已采用版本约束。
3. 导出改为临时 ZIP 文件、`shutil.copyfileobj` 流式写入和响应关闭清理；照片、名单、文档缺失写入异常记录而不中断整个 ZIP。
4. 统计单元格保留整数类型；原始照片只复制到导出目录，默认姓名、男 01 / 女 02、同名后缀规则保持不变。

