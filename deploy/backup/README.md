# 学生资料备份、恢复与清空

备份只写入独立的私有目录，不放入 Nginx 静态目录。备份 ZIP 包含：

- `db.sqlite3`：通过 SQLite online backup API 生成的数据库快照；
- `media/`：名单、照片、文档和上传暂存文件；
- `MANIFEST.json`：每个媒体文件的大小和 SHA256、数据库快照 SHA256、备份格式和时间。

## 创建并验证备份

```bash
docker compose exec web python manage.py backup_collection --output /app/data/backups/student-collector-$(date +%Y%m%d-%H%M%S).zip
```

命令只有在 ZIP CRC、数据库和媒体清单全部通过后才报告成功；失败时会删除临时 ZIP。

## 隔离恢复演练

先停止写入（生产环境先停止 web/nginx 或关闭公开上传），再执行：

```bash
docker compose stop web nginx
docker compose run --rm web python manage.py restore_collection --input /app/data/backups/<备份文件>.zip --yes
docker compose up -d web nginx
```

恢复前会再次读取 ZIP 并校验 CRC、路径穿越和所有 SHA256。恢复目标使用临时文件/目录替换，失败时尝试回滚原数据库和媒体目录。

## 清空全部采集资料

现有后台“清空全部采集数据”按钮仍由 E 线接入本模块。独立安全命令如下：

```bash
docker compose exec web python manage.py clear_collection_data \
  --backup /app/data/backups/before-clear-$(date +%Y%m%d-%H%M%S).zip --yes
```

清空命令会先完成并验证备份，再关闭公开上传，将媒体目录移动到同文件系统隔离区，提交数据库删除事务，事务成功后才删除隔离区。管理员账号不删除；数据库异常时恢复媒体目录并恢复公开上传设置。进程在提交后、隔离区清理前中断时，保留已验证备份和隔离区，禁止继续覆盖，需人工按备份恢复流程处理。

生产环境必须将 `/app/data/backups` 复制到独立存储，并定期做隔离目录恢复演练。备份、恢复和清空命令不应在测试期间指向学校生产路径。
