# Student Collector R3-A 自审与联验报告

基线：`4e0a9927e0c0d4e33ef94a2692b33ac34473fc4b`
分支：`codex/r3-a-upload-resume`
环境：隔离 Docker Compose + Nginx，虚构学生数据；未访问生产环境。

## 本轮修复

- 前端使用 XHR 真实上传进度；照片按最多 20 张、最多 32 MB 自动分批。
- 每批最多自动重试 3 次，服务端批次按序号和内容摘要幂等；失败后可只重试未确认部分。
- 上传会话状态接口受上传令牌保护，返回已确认批次、文件数量、文件名、大小和 SHA256；刷新后重新选择文件，清单内容必须完全一致才允许继续。
- Excel、照片和文档清单均在会话中保存；文档也按名称、顺序、大小和 SHA256 强校验，避免为了分批而绕过混合资料校验。
- 上传会话创建使用幂等键；响应丢失或进程在名单落盘前退出时，重试可以恢复同一会话。
- 浏览器端幂等键改为每次上传操作生成的 Web Crypto 随机值；重试保留原值，相同文件的新提交会创建新的会话和私有凭证；服务端同时校验名单 SHA256、原始文件名、Excel 指纹、照片/文档清单摘要、数量、备注和会话状态。
- 上传状态改为只接受 POST 请求体中的令牌，并返回 `no-store`/`no-cache`；Nginx 日志检查未发现 `upload_token` 查询参数。
- 提交结果页支持凭提交编号和私有凭证补传缺失照片；补传先进入待审核版本，同名不随机匹配，管理员采用后才写入有效照片。
- 补传人工绑定使用事务锁和数据库部分唯一约束，同一补传不能把两张照片绑定到同一学生，支持解除/纠正绑定；审核前再次校验学生归属、已有照片和当前有效提交版本。
- 原提交被新版本替代时，旧补传安全标记为“原提交已失效”并保留文件与审计记录，最终导出只读取当前有效采用版本。
- 保留管理员清空全部采集资料功能。

## RED → GREEN

1. 混合上传的原实现要求首个 `/start/` 请求同时携带文档；浏览器分批实现不携带文档时会被后端判为数量不一致（RED）。修复后文档清单随会话保存、文档独立分批并强校验，混合 HTTP 验收通过（GREEN）。
2. 原实现只有照片清单强校验，文档清单不能在刷新后核对内容（RED）。新增文档清单测试，名称、顺序、大小、SHA256 不一致会拒绝；新会话状态测试通过（GREEN）。
3. 原实现幂等启动在会话已创建但名单尚未落盘时直接返回，会导致后续无法完成（RED）。增加缺失名单恢复和元数据一致性校验；幂等启动测试通过（GREEN）。
4. R3-A 初始浏览器检查使用旧示例名单，服务端正确返回逐行 Excel 错误（RED，输入数据问题）；换用当前模板后真实浏览器提交、分批、最终化和结果页全部通过（GREEN）。
5. F1：同文件复用同一幂等键可恢复，修改清单/指纹会拒绝；不同随机键得到不同会话/凭证，完成后同文件新提交也得到独立会话（GREEN）。
6. F2：旧 GET 状态查询被拒绝（405）；POST 状态查询带 `no-store`/`no-cache`，真实 Nginx 日志未出现令牌查询参数（GREEN）。
7. F3：同一补传重复绑定先以唯一约束/事务校验拒绝，人工绑定可解除后重新选择；重复审核返回业务错误而不是 500（GREEN）。
8. F4：原提交审核后被同班新版本替代，旧补传采用请求被标记 `expired`，不产生旧版本照片（GREEN）。

## 测试退出码

| 验证 | 结果 | 退出码 |
|---|---:|---:|
| `pytest -q`（完整本地回归） | 63 passed | 0 |
| `python manage.py check` | 无问题 | 0 |
| `python manage.py makemigrations --check --dry-run` | No changes detected | 0 |
| `pytest`：50/100/300 参数化、10 班并发、同班审核竞争、R2 并发隔离 | 6 passed | 0 |
| `node --check static/js/app.js` | 通过 | 0 |
| `git diff --check` | 通过 | 0 |
| Docker + Nginx：10 班并发，每班 20 张，混合 15 张大照片 + 20 MB PDF | 通过；DB/磁盘/哈希/ZIP/Excel | 0 |
| Docker + Nginx：50 张约 2.5 MB 照片 | 通过；5 批、ZIP 哈希/CRC/统计 | 0 |
| Docker + Nginx：100 张约 3 MB 照片 | 通过；12 批、100 DB 照片、100 磁盘照片、ZIP 100 张 | 0 |
| Docker + Nginx：300 张批次边界 | 通过；15 批、DB 300、ZIP/统计一致 | 0 |
| Docker + Nginx：缺失照片补传、管理员审核、ZIP | 通过；4→5 张，pending→approved | 0 |
| Docker + Nginx：300 张按最多 20 张/批上传 | 通过；15 批、DB 300、ZIP 300、CRC/哈希/分类 | 0 |
| Playwright 真实浏览器：21 张照片、临时失败重试、刷新后重选恢复 | 通过；选择数 21，结果页 21 名学生/21 张照片 | 0 |
| Nginx 访问日志令牌检查 | `upload_token` 查询参数不存在 | 0 |

关键实测输出：

- `HTTP_R2_FINAL_OK mixed=15+20MB-doc concurrent_classes=10 concurrent_photos=200 total_photos=215 zip_bytes=50687703`
- `HTTP_R3_50_LARGE_OK photos=50 batches=5 zip_bytes=167571325 elapsed_seconds=20.41`
- `HTTP_R2_100X3_OK photos=100 batches=12 db_photos=100 media_photo_files=100 zip_photos=100 zip_bytes=335109949 elapsed_seconds=45.47 client_maxrss_kb=871200 media_bytes=339126137`
- `HTTP_R3_300_BATCH_OK photos=300 batches=15 db_photos=300 zip_photos=300 zip_bytes=226042`
- `HTTP_R3_SUPPLEMENT_OK submission=40165cfa-04c0-401b-899f-9738dc139c54 pending_to_approved=1 photos=5 zip_bytes=26216`
- `NGINX_UPLOAD_TOKEN_QUERY_ABSENT NGINX_LOG_LINES=39`

浏览器快照确认 Excel 显示“已选择：r3a-browser.xlsx”、照片显示“已选择 21 个文件”；注入一次临时 503 后自动重试，刷新页面并重新选择同一组文件后继续未完成批次，最终结果页显示 21 名学生均已匹配；服务端核对 `students=21, photos=21, staged=21`。

## 未完成 / 约束

- 本轮未合并、未部署、未触发 GitHub Actions。
- 生产环境 HTTPS、真实学校数据和公网访问仍需独立部署验收；本报告只证明隔离环境的本地/Docker/Nginx 行为。
- 300 张 HTTP 验收夹具曾因名单使用 `:02d`、照片误用 `:03d` 造成前 100 张无法按姓名匹配（RED）；修正夹具后 15 批、300 条数据库照片和 300 张 ZIP 照片全部通过（GREEN）。
