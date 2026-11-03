# 赤峰一中学生资料采集系统 V1

独立、单实例部署的学校学生资料采集系统。它与父目录 `cfyz`、EasyPrinter 及任何现有生产数据库/服务无关；本项目只使用自己的 SQLite 数据库和私有文件目录。

## 已实现范围

- `/submit`：统一公开入口，无需注册登录；Excel 名单、批量 JPG/PNG、可选相关文档上传。
- 大批量照片由浏览器自动拆成每批不超过 20 张且不超过 32 MB 的可重试上传会话；服务端批次具有幂等凭证，断网后可在当前页面继续提交。
- Excel 模板下载、逐行必填/枚举/重复检查；每次提交生成独立版本、随机修改凭证。
- 匹配方式：照片默认按唯一学生姓名匹配；同名学生不随机匹配，标记为需人工确认。
- `/admin/`：独立管理员登录、提交版本审核/退回/采用、进度概览、学年班级范围、公开上传开关。
- Excel 只需填写姓名、性别、学部、学年、班级、就读类型和备注；年级按学部与学年自动判断（2026/2025/2024 对应一/二/三年级）。
- 仅按最终“已采用”版本统计和导出；支持学部、学年、班级、性别、就读类型（走读、半走读、住宿）、照片状态筛选。
- ZIP 全部类别导出时，每个班级目录自动分为 `走读/`、`半走读/`、`住宿/`，每类包含 `学生统计表.xlsx`、`学生照片/`；原始学生名单放入班级目录的 `学生名单/`。
- 默认按姓名命名；支持姓名+性别编号、自定义字段组合、实时预览、非法路径字符过滤、重名自动后缀。
- 文件放在 Web 根目录之外，不提供公开媒体下载；导出不落公共静态目录；管理员操作写入审计记录。
- Docker Compose + Gunicorn + Nginx；SQLite/私有媒体目录可独立备份。
- 上传协调参数：单文件 20 MB、单批 32 MB、单批最多 20 张照片、Django 单请求最多 40 个文件、Nginx 40 MB、Gunicorn 300 秒；`MAX_STORAGE_MB` 控制私有媒体配额，导出临时 ZIP 使用 `EXPORT_TEMP_DIR`。

## 本地开发

PowerShell：

```powershell
cd E:\cfyz\student-collector-v1
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py create_admin admin
.\.venv\Scripts\python.exe manage.py make_sample_data
.\.venv\Scripts\python.exe manage.py runserver
```

访问 `http://127.0.0.1:8000/submit`，后台访问 `http://127.0.0.1:8000/admin/`。

开发环境可把 `.env` 中 `DJANGO_DEBUG=1`，生产环境必须设置长随机 `DJANGO_SECRET_KEY`、正确的 `DJANGO_ALLOWED_HOSTS`、HTTPS 域名的 `CSRF_TRUSTED_ORIGINS`，并使用 HTTPS 反向代理。

## Docker 部署

1. 复制 `.env.example` 为 `.env`，填入长随机密钥、正式域名、`SITE_URL` 和 `DJANGO_ALLOWED_HOSTS`；不要把真实 `.env` 提交到 Git。
2. 先准备持久化目录：`data/` 保存 SQLite，`media/` 保存私有原始文件。
3. 执行：

```powershell
docker compose up -d --build
docker compose exec web python manage.py create_admin admin
```

示例 Compose 暴露 `8080`，生产 Nginx/网关应终止 HTTPS 并转发到该端口；`SITE_URL` 用于生成提交结果链接，必须填写实际访问地址。部署后检查 `/submit`、`/admin/login/`，确认没有公开 `/media/` 路由。

如果只是内网 IP 验收，可将 `.env` 中 `HTTPS_ENABLED=0`，并把 `CSRF_TRUSTED_ORIGINS` 设置为 `http://192.168.88.99:8080`；正式生产必须恢复 `HTTPS_ENABLED=1` 并通过 HTTPS 访问。

## 备份、恢复和清理

备份 SQLite 和私有文件：

```powershell
.\.venv\Scripts\python.exe manage.py backup_data --output backups
```

Docker 环境：`docker compose exec web python manage.py backup_data --output /app/data/backups`，再把备份复制到独立存储。恢复时先停止写入/停止容器，解压备份中的 `db.sqlite3` 到 `DATABASE_PATH` 指定位置，将 `media/` 恢复到 `MEDIA_ROOT`，再启动并执行 `migrate`。恢复后登录后台核对学生、照片和导出结果。

到期清理默认只清理超过 `DATA_RETENTION_DAYS` 的“已退回”提交，已采用数据不会被默认删除：

```powershell
.\.venv\Scripts\python.exe manage.py cleanup_expired
.\.venv\Scripts\python.exe manage.py cleanup_expired --yes
```

学校应在部署前确认未成年人资料的合法处理依据、授权范围、保存期限、备份保留期和管理员名单。

## 测试与证据

本地隔离测试：

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe -m pytest -q
```

R1 可靠性测试：

```powershell
docker compose build web
docker compose run --rm -v "${PWD}:/app" web python manage.py check
docker compose run --rm -v "${PWD}:/app" web python manage.py makemigrations --check --dry-run
docker compose run --rm -v "${PWD}:/app" web pytest -q
```

`tests/test_reliability_r1.py` 覆盖 50/100/300 张照片分批上传、数据库/磁盘/哈希一致、批次重试幂等、10 班并发提交、同班审核竞争、缺失照片异常导出、数值统计和流式临时 ZIP。Docker + Nginx 真 HTTP 联验脚本为 `tests/http_r1.py`，需要在隔离 Compose 环境中运行，并核对 50 张照片、ZIP 内容和统计 Excel，而不是只看状态码。GitHub Actions 未启动；本地可复现测试是当前验收证据，合并和部署仍是独立门禁。

## 安全边界与后续门禁

- V1 不接受 ZIP 上传；图片通过 Pillow 实际解码校验，文档检查扩展名/文件头/大小。
- 上传页不提供其他班级检索和下载；提交结果只接受对应随机凭证。
- SQLite 适用于 V1 单实例；部署前要做并发/备份恢复/HTTPS/限流压测和真实数据脱敏检查。
- 当前实现的上传数量/大小限制是应用层、Django、Nginx 三层协调限制；大批量照片必须走分批接口，不通过无限提高单请求大小解决。
- 不要直接使用 `django-admin/` 作为对外入口；它保留给本地维护，正式管理入口是 `/admin/`。
- 未提供自动公网开放、支付、微信小程序或现有系统迁移；这些不属于 V1 范围。

