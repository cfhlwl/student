# R3-B/R3-C 最终集成自审报告

基线：R3-A `b80ac31438a6ee44e3544c421273b9d620ef030c`

最新集成：`942c728af8cc9a5d75605d8d9f62c75499b9a3a9`

最新树：`1484afb800e3c66773556b2b4e3e60255b2af586`

## 工作线提交

| 线 | Draft PR | Exact HEAD | TREE | 门禁 |
|---|---|---|---|---|
| A 预检查 | #5 | `ec54e43c7d8b9e5d0406abfb2f66e7edfe7efc47` | `a6cd27b5ac0a26787dbe2c173fbf14d22e28ba0c` | PASS |
| B 管理统计 | #4 | `3046dca78021302cd72e74564d3a459185b3822f` | `25e0f8b2e709e67e109d60dbf4883cdc847e5eca` | PASS |
| C UI | #6 | `1f97b46801e79c4f9ad8fa850179366edae733c0` | `1a174dd1c6e6340d3499d9d092c849c8e4404e08` | PASS |
| D 安全/备份 | #7 | `1ffeb815de3b1f47885da99cb9eeaec1463d5b50` | `3f1f91234603a541dc1662ca09954b663c13bf92` | PASS_SCOPED |
| E 集成 | #8 | `942c728af8cc9a5d75605d8d9f62c75499b9a3a9` | `1484afb800e3c66773556b2b4e3e60255b2af586` | PASS_LOCAL |

## RED → GREEN

- A/B/C/D 各专项保留了独立 RED 失败记录及 GREEN 退出码。
- 集成后的 Docker 全量：`86 passed`，exit `0`。
- 集成专项（安全/备份、预检查、管理统计、UI、跨学年、清空）：`9 passed`，exit `0`。
- Django check、迁移检查、Python compileall、三个 JavaScript 语法检查、`git diff --check` 均 exit `0`。

## 真实联验

- Docker + Nginx：10 班并发，10×20 张照片，混合文档，215 张总导出；数据库、媒体文件、SHA256、ZIP CRC、统计表通过，exit `0`。
- Docker + Nginx：100×约 3MB JPEG，12 批，数据库/磁盘/ZIP 均 100 张；失败批次、重试、重复请求、管理员审核和 ZIP SHA256/CRC 通过，exit `0`。
- Docker + Nginx：300 张小型虚构 JPEG，按 20 张一批共 15 批，数据库和 ZIP 均 300 张，分类统计通过，exit `0`。
- 50 张、R2/R3-A 的 300 边界及补传矩阵沿用 R3-A 已验收证据，并由当前 86 项回归覆盖服务端契约。
- Playwright 桌面和 390×844 手机页通过；拍摄示例弹窗可打开，上传页文件选择提示、导航和按钮可见。截图保存在 `output/playwright/`。
- Docker 备份/恢复：SQLite online backup、媒体 SHA256 清单、ZIP CRC 和 Docker `/app/media` 挂载点恢复通过，exit `0`。
- 清空验收：曾发现 UploadSession/StagedUpload 残留，已修复；最新专项测试确认提交、学生、照片、文档、补传、上传会话、暂存记录和媒体内容清零，管理员账号保留。

## 关键集成修复

1. 预检查接入 `/submit/preflight/` 和受 CSRF 保护的检查接口；照片预检查也按 20 张/32MB 分批，避免大请求触发 Nginx 413。
2. 管理员采集统计及异常名单 XLSX 接入独立权限保护，统计只取当前有效采用版本。
3. 导出目录加入学年层级：`学年/学部/年级/班级/就读类型`，防止不同学年同名班级覆盖。
4. 清空全部资料改为先备份、媒体隔离、数据库提交后删除，并清理上传会话、批次、暂存文件和补传记录。
5. 恢复逻辑保留 Docker 媒体挂载点，不删除挂载目录本身。

## 门禁

- `MERGE=HOLD_FOR_CHATGPT`
- `DEPLOY=NO`
- `GITHUB_ACTIONS=NO`
- 未访问生产服务器、生产数据库或其他项目；所有真实联验使用隔离 Docker、虚构学生数据和临时卷。
