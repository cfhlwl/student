# R3-B/R3-C 最终集成自审报告

基线：R3-A `b80ac31438a6ee44e3544c421273b9d620ef030c`

PR #8 上一审核 HEAD：`0917aa5e2b8fa6c3908bb50365e777bfc611c9f1`

本轮源码提交：`768111e361ca8d6176ed90bec73271ec914ebe49`

本轮源码树：`d15929f8cdf9df7bc07b3fd3483e9fe46ef35b3b`

本报告将在源码提交后以独立 docs-only 提交追加；最终 PR HEAD 以交付消息中的 `PR8_NEW_EXACT_HEAD` 为准。

## 工作线提交

| 线 | Draft PR | Exact HEAD | TREE | 门禁 |
|---|---|---|---|---|
| A 预检查 | #5 | `ec54e43c7d8b9e5d0406abfb2f66e7edfe7efc47` | `a6cd27b5ac0a26787dbe2c173fbf14d22e28ba0c` | PASS |
| B 管理统计 | #4 | `3046dca78021302cd72e74564d3a459185b3822f` | `25e0f8b2e709e67e109d60dbf4883cdc847e5eca` | PASS |
| C UI | #6 | `1f97b46801e79c4f9ad8fa850179366edae733c0` | `1a174dd1c6e6340d3499d9d092c849c8e4404e08` | PASS |
| D 安全/备份 | #7 | `1ffeb815de3b1f47885da99cb9eeaec1463d5b50` | `3f1f91234603a541dc1662ca09954b663c13bf92` | PASS_SCOPED |
| E 集成（F2-A/F2-B 源码） | #8 | `768111e361ca8d6176ed90bec73271ec914ebe49` | `d15929f8cdf9df7bc07b3fd3483e9fe46ef35b3b` | PASS_LOCAL |

## RED → GREEN

- A/B/C/D 各专项保留了独立 RED 失败记录及 GREEN 退出码。
- 审查基线定向 RED：在 `0917aa5e2b8fa6c3908bb50365e777bfc611c9f1` 上运行新增 F2 用例，因缺少统一状态函数收集失败，exit `1`。
- 本轮 F2-A/F2-B 定向 GREEN：`3 passed`，exit `0`。
- 集成后的 Docker 全量：`96 passed`，exit `0`。
- F1～F5 定向测试：`7 passed`，exit `0`；首轮 RED 包含旧测试未传密码、预检查静态文件未加载和错误测试编号，均已修正后复跑 GREEN。
- Django check、迁移检查、Python compileall、JavaScript 语法检查、`git diff --check` 均 exit `0`。

## 真实联验

- 本轮隔离 Docker + 双 Worker Gunicorn 真实 HTTP：100×约 3MB JPEG，12 批，数据库/磁盘/ZIP 均 100 张；失败批次、重试、重复请求、管理员审核和 ZIP SHA256/CRC 通过，exit `0`，ZIP `335111002` 字节。
- 本轮隔离 Docker + 双 Worker Gunicorn 真实 HTTP：300×约 3MB JPEG，预检查 300 张、正式上传 34 批，数据库/磁盘/ZIP 均 300 张；ZIP `1005305318` 字节，CRC 与统计表通过，exit `0`。
- 本轮隔离 Docker + 双 Worker Gunicorn 真实 HTTP：10 班并发、每班 20 张并混合 20MB 文档；总导出 215 张，数据库、媒体、SHA256、ZIP CRC、统计表通过，exit `0`，ZIP `50690460` 字节。
- Nginx 配置校验和此前 Nginx 联验保持在上一集成证据中；本轮未修改 Nginx。当前 Docker Desktop 对 E: 盘 bind mount 报 `/run/desktop/mnt/host/e` 创建错误，因此本轮大文件补证使用隔离容器内 Gunicorn，未将该限制冒充为 Nginx 实测通过。
- 50 张、R2/R3-A 的边界及补传矩阵由当前 93 项回归覆盖服务端契约。
- Playwright 桌面和 390×844 手机页通过；继续提交按钮实际跳转 `/submit?from=preflight`，并提示重新选择文件。截图保存在 `output/playwright/`。
- Docker 备份/恢复：SQLite online backup、媒体 SHA256 清单、ZIP CRC 和 Docker `/app/media` 挂载点恢复通过，exit `0`。
- 清空验收：错误/缺失密码均零数据库和媒体修改；正确密码先取得跨 Worker 独占维护门禁、生成唯一不覆盖的备份，成功后保持关闭，管理员账号保留；失败注入会从已校验备份恢复 SQLite 与媒体。

## 关键集成修复

1. 预检查接入 `/submit/preflight/` 和受 CSRF 保护的检查接口；照片预检查也按 20 张/32MB 分批，避免大请求触发 Nginx 413。
2. 管理员采集统计及异常名单 XLSX 接入独立权限保护，统计只取当前有效采用版本。
3. 导出目录加入学年层级：`学年/学部/年级/班级/就读类型`，防止不同学年同名班级覆盖。
4. 清空/备份/恢复统一使用跨 Worker 文件锁；普通上传、补传、人工匹配、审核写操作均受共享写门禁保护，清空成功后状态文件保持 `closed`，只允许管理员明确重开。
5. 清空备份使用 UUID 后缀且目标存在时拒绝覆盖；失败时使用已校验快照恢复 SQLite 与媒体。
6. Nginx 将 `X-Forwarded-For` 覆盖为 `$remote_addr`；预检查单独使用 120/60 秒有界配额，300 张大照片不与普通提交配额冲突。
7. 预检查页“继续提交”已接通，并明确提示浏览器不会跨页面保留文件。

## 本轮门禁

| 门禁 | 结果 |
|---|---|
| E-F1 密码二次验证 | PASS：错误/缺失零修改，正确密码可清空，管理员保留 |
| E-F2 维护冻结/备份/清空/恢复 | PASS：跨线程排空、唯一备份、失败回滚、成功闭锁 |
| E-F3 可信代理/限流 | PASS：Nginx XFF 覆盖；伪造 XFF 仍在第 21 次返回 429 |
| E-F4 300×3MB 预检查/上传 | PASS：300 张预检查、34 批正式上传、SHA256/ZIP/Excel 对账 |
| E-F5 继续提交操作 | PASS：桌面/390×844 手机实测跳转并提示重选 |

### F2-A / F2-B 收尾门禁

| 门禁 | 结果 |
|---|---|
| CLEAR_SUCCESS_GATE | PASS：清空成功后状态文件为 `closed`，公开上传返回 403 |
| ADMIN_DISPLAY_GATE | PASS：后台统一状态读取显示“开启公开上传” |
| SINGLE_REOPEN_GATE | PASS：一次 POST 同时恢复状态文件和 AppSetting，上传恢复 200 |
| CLEAR_FAILURE_ROLLBACK_GATE | PASS：既有失败回滚用例仍通过，状态与媒体恢复 |
| ADMIN_NAMING_WRITE_GATE | PASS：关闭/维护期间无命名规则写入，返回可理解页面提示 |
| ADMIN_CLASSES_WRITE_GATE | PASS：关闭/维护期间无班级或学年写入 |
| UPLOAD_TOGGLE_RACE_GATE | PASS：开关使用独占维护门禁，与清空/备份串行 |
| MAINTENANCE_READ_ONLY_GATE | PASS：命名页 GET 不创建 NamingRule |
| MULTI_WORKER_LOCK_GATE | PASS：跨线程共享写与独占维护锁排空测试通过 |

说明：截图与浏览器日志仅保存在本次隔离工作树的 `output/playwright/`，未作为生产资料或公共静态文件发布。

## 门禁

- `MERGE=HOLD_FOR_CHATGPT`
- `DEPLOY=NO`
- `GITHUB_ACTIONS=NO`
- 未访问生产服务器、生产数据库或其他项目；所有真实联验使用隔离 Docker、虚构学生数据和临时卷。
