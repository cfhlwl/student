# R3-C D 线：安全、导出及备份报告

基线：`b80ac31438a6ee44e3544c421273b9d620ef030c`
工作树：`E:\cfyz\student-collector-v1-r3-c`
测试数据：全部为隔离环境虚构数据；未访问生产服务器、生产数据库或其他项目。

## 已完成

### D1 可信代理

- 新增 `TrustedProxyMiddleware`，只有 `TRUSTED_PROXY_NETWORKS` 中的 Nginx/Django 直接对等端才可解释 `X-Forwarded-For`、`X-Real-IP`、`X-Forwarded-Proto`。
- 非可信直连请求会清理转发头，不能伪造来源 IP 或 HTTPS 判断。
- Nginx 增加 `X-Forwarded-For $proxy_add_x_forwarded_for`；Docker 中 web 不发布宿主机端口，`.env.example` 要求显式配置可信代理网段。

### D2 共享限流

- 新增 `CollectionRateLimitMiddleware` 和 `collector/security_controls.py`。
- 覆盖整页提交、上传会话创建、照片批次、文档批次、状态查询、complete、补传。
- 使用 Django file-based cache，目录位于 `/app/data/rate-limit-cache`，跨 Gunicorn worker 共享，不写 SQLite 高频计数。
- 超限返回 429、`Retry-After` 和 `Cache-Control: no-store`。
- 测试环境使用自动清理限流缓存 fixture，保证用例隔离；生产不会清理缓存。

### D4 清空保护

- 新增 `clear_collection_safely()` 和 `clear_collection_data` 命令。
- 先做已验证备份，再关闭公开上传；媒体目录先移动到同文件系统隔离区，数据库删除成功后才删除隔离区。
- 数据库异常会恢复隔离媒体和原公开上传状态；管理员账号不触碰。
- 原有后台清空按钮保留。由于本线明确不得修改 `collector/views.py`、模板和 URL，后台按钮尚未接入本安全命令，接线点已列在下方。

### D5 备份恢复

- 新增 `collector/backup.py`、`backup_collection`、`restore_collection`。
- SQLite 使用 online backup API；媒体、数据库快照和 SHA256 清单一起写入临时 ZIP。
- 写入失败、CRC 失败、路径穿越、大小/SHA256 不一致均不会发布成功 ZIP。
- 恢复先完整校验，再用临时数据库和临时媒体目录替换；异常尝试回滚。
- 操作说明见 `deploy/backup/README.md`。

## RED → GREEN 证据

| 项目 | RED 退出码/现象 | 修复后 GREEN |
|---|---:|---:|
| 备份/恢复定向测试 | `1`，清空测试无法处理 Django memory DB，恢复数据库字节比较不成立 | `0`，`4 passed` |
| 全量回归首次运行 | `1`，`66 passed, 2 failed`；新限流计数跨测试残留导致旧上传用例得到 429 | `0`，`68 passed` |
| 可信代理伪造头 | `1`（未实现前定向用例失败） | `0`，可信对等端使用头、直连清理头 |
| 共享限流 | `1`（未实现前无共享控制） | `0`，同 IP 第 3 次 `/submit/upload/start/` 返回 429 |

定向和全量均在 Docker 隔离容器中执行。最终命令：

```text
docker run --rm ... pytest -q tests/test_security_backup.py  -> exit 0, 4 passed
docker run --rm ... pytest -q                              -> exit 0, 68 passed
```

## 未完成与 E 线接线点

1. **跨学年 ZIP 目录尚未在本线改动。** 当前 `collector/services.py::export_students_to_tempfile` 使用的 `class_dir` 是“学部/年级/班级”，未包含“学年”。由于用户指定本线不得修改 `services.py`，本线没有覆盖该文件。E 线必须在合并时将目录前缀改为“学年/学部/年级/班级”，并补充两个学年同名班级的 ZIP 隔离测试；否则 `D3` 不能宣告 GREEN。
2. **后台清空按钮接线。** E 线需要把现有 `clear_test_data` POST 接到 `clear_collection_safely`，要求页面显示二次确认、备份路径/备份提示，并保留管理员账号。当前独立命令已可安全执行，原按钮未删除。
3. **限流参数需按学校真实班级规模复核。** 默认值允许正常的 300 张照片分批上传，但正式部署仍需以真实 Nginx/Gunicorn worker 数和班级峰值压测校准。
4. 恢复命令默认要求停止 web 写入；正式生产恢复必须由部署人员执行停写、备份核验和重启后的抽样核对。

## 风险结论

D1、D2、D4 独立安全模块和 D5 备份恢复模块已通过本地 Docker 测试。D3 跨学年导出和后台按钮接线因文件所有权限制留给 E 线，不应在当前提交上宣称 R3-C 全部集成完成。
