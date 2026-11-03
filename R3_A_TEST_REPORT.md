# Student Collector R3-A 自审与联验报告

基线：`ee914a88aec7bc734e26101f527e26b24d502332`
分支：`codex/r3-a-upload-resume`
环境：隔离 Docker Compose + Nginx，虚构学生数据；未访问生产环境。

## 本轮修复

- 前端使用 XHR 真实上传进度；照片按最多 20 张、最多 32 MB 自动分批。
- 每批最多自动重试 3 次，服务端批次按序号和内容摘要幂等；失败后可只重试未确认部分。
- 上传会话状态接口受上传令牌保护，返回已确认批次、文件数量、文件名、大小和 SHA256；刷新后重新选择文件，清单内容必须完全一致才允许继续。
- Excel、照片和文档清单均在会话中保存；文档也按名称、顺序、大小和 SHA256 强校验，避免为了分批而绕过混合资料校验。
- 上传会话创建使用幂等键；响应丢失或进程在名单落盘前退出时，重试可以恢复同一会话。
- 提交结果页支持凭提交编号和私有凭证补传缺失照片；补传先进入待审核版本，同名不随机匹配，管理员采用后才写入有效照片。
- 保留管理员清空全部采集资料功能。

## RED → GREEN

1. 混合上传的原实现要求首个 `/start/` 请求同时携带文档；浏览器分批实现不携带文档时会被后端判为数量不一致（RED）。修复后文档清单随会话保存、文档独立分批并强校验，混合 HTTP 验收通过（GREEN）。
2. 原实现只有照片清单强校验，文档清单不能在刷新后核对内容（RED）。新增文档清单测试，名称、顺序、大小、SHA256 不一致会拒绝；新会话状态测试通过（GREEN）。
3. 原实现幂等启动在会话已创建但名单尚未落盘时直接返回，会导致后续无法完成（RED）。增加缺失名单恢复和元数据一致性校验；幂等启动测试通过（GREEN）。
4. R3-A 初始浏览器检查使用旧示例名单，服务端正确返回逐行 Excel 错误（RED，输入数据问题）；换用当前模板后真实浏览器提交、分批、最终化和结果页全部通过（GREEN）。

## 测试退出码

| 验证 | 结果 | 退出码 |
|---|---:|---:|
| `pytest -q`（完整本地回归） | 58 passed | 0 |
| `python manage.py check` | 无问题 | 0 |
| `python manage.py makemigrations --check --dry-run` | No changes detected | 0 |
| `node --check static/js/app.js` | 通过 | 0 |
| `git diff --check` | 通过 | 0 |
| Docker + Nginx：10 班并发，每班 20 张，混合 15 张大照片 + 20 MB PDF | 通过；DB/磁盘/哈希/ZIP/Excel | 0 |
| Docker + Nginx：50 张约 2.5 MB 照片 | 通过；5 批、ZIP 哈希/CRC/统计 | 0 |
| Docker + Nginx：100 张约 3 MB 照片 | 通过；12 批、100 DB 照片、100 磁盘照片、ZIP 100 张 | 0 |
| Docker + Nginx：300 张批次边界 | 通过；15 批、DB 300、ZIP/统计一致 | 0 |
| Docker + Nginx：缺失照片补传、管理员审核、ZIP | 通过；4→5 张，pending→approved | 0 |
| Playwright 真实浏览器：选择 Excel/2 张照片、提交、结果页 | 通过；start/photos/complete 均 HTTP 200 | 0 |

关键实测输出：

- `HTTP_R2_FINAL_OK mixed=15+20MB-doc concurrent_classes=10 concurrent_photos=200 total_photos=215 zip_bytes=50687726`
- `HTTP_R3_50_LARGE_OK photos=50 batches=5 zip_bytes=167571325 elapsed_seconds=20.41`
- `HTTP_R2_100X3_OK photos=100 batches=12 db_photos=100 media_photo_files=100 zip_photos=100 zip_bytes=335109950 elapsed_seconds=45.00 client_maxrss_kb=867788 media_bytes=339126137`
- `HTTP_R3_300_BATCH_OK photos=300 batches=15 db_photos=300 zip_bytes=225764`
- `HTTP_R3_SUPPLEMENT_OK pending_to_approved=1 photos=5 zip_bytes=26217`

浏览器快照确认 Excel 显示“Excel 文件已选择 / 已选择：r3-template.xlsx”，照片显示“文件已加入，可继续调整选择 / 已选择 2 个文件”；最终请求为 `/start/`、`/photos/`、`/complete/`，结果页加载成功，最终控制台无错误。

## 未完成 / 约束

- 本轮未合并、未部署、未触发 GitHub Actions。
- 生产环境 HTTPS、真实学校数据和公网访问仍需独立部署验收；本报告只证明隔离环境的本地/Docker/Nginx 行为。
