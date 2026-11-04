# Student Collector V2：G3 Windows/Linux/Docker 安装计划

## 1. 交付形态

| 形态 | 目标 | 是否依赖 Docker | 服务管理 |
| --- | --- | --- | --- |
| Windows x64 原生安装包 | 校内 Windows 10/11、Windows Server x64 | 否 | Windows Service（WinSW 或等价受维护组件） |
| Linux x64 安装包 | Debian/Ubuntu/RHEL 系列服务器 | 否 | systemd；可选 Nginx |
| Docker Compose 包 | 现有 NAS/Linux 部署和升级 | 是 | Compose restart policy |
| 离线包 | 无公网服务器 | 否（原生包）/可带镜像（Docker） | 同上 |

“安装脚本 + 预装 Docker Desktop”不算 Windows 原生安装包。原生包必须携带或安装已校验的 Python 运行时、依赖 wheel、静态文件和服务包装器。

## 2. 统一目录契约

应用代码和数据必须分离：

```text
应用目录/                 可替换，只读
  app/
  runtime/                原生包携带的 Python/venv
  migrations/
数据目录/                 持久化，不随卸载删除
  db/db.sqlite3
  media/
  exports/
  backups/
  logs/
  locks/
```

Windows 默认 `%ProgramData%\StudentCollector\`，服务账户仅授予数据目录读写；Linux 默认 `/var/lib/student-collector/`，日志 `/var/log/student-collector/`，配置 `/etc/student-collector/`。Docker 继续使用 Compose 中的 `data/`、`media/` 挂载。

所有路径由环境变量或安装向导写入配置，应用内部只使用 `pathlib` 和相对 storage key。禁止把用户输入直接拼接为系统路径。

## 3. Windows 原生安装

安装包建议使用 Inno Setup/WiX 生成，包含：

- 签名的 x64 Python 3.12 运行时或嵌入式运行时；
- 已锁定哈希的离线 wheelhouse；
- Django 项目、静态文件、迁移和服务包装器；
- 首次安装向导：管理员初始化、监听地址/端口、数据目录、站点 URL、HTTPS 反向代理提示；
- 服务安装/启动/停止、健康检查和防火墙规则预览；
- 升级、备份、失败回滚、卸载保留数据选项。

服务默认绑定 `127.0.0.1`，如需局域网访问由安装向导明确选择并显示防火墙影响；正式 HTTPS 由 IIS/Nginx/现有网关终止，应用不自动生成不受信任证书。

维护锁使用跨进程 Windows 文件锁（建议 `portalocker`，或经过测试的 `msvcrt` 封装），不能依赖当前线程锁回退。服务账户不能拥有应用目录写权限，只拥有数据、日志和临时目录所需权限。

## 4. Linux 原生安装

离线 tar 包或发行版包执行：


1. 创建专用系统用户和数据目录。
2. 解包应用和 wheelhouse，创建隔离 venv。
3. 写入 `/etc/student-collector/student-collector.env`，权限 `0600`。
4. 执行迁移计划检查和健康检查。
5. 安装 systemd unit、日志轮转和可选 Nginx 配置。
6. 初始化管理员后才允许打开管理入口。

升级采用 `releases/<version>` + `current` 原子指针：先备份，预检查迁移，切换服务，访问健康端点和数据库读写检查；失败则恢复旧指针和旧配置。卸载默认停止服务并删除应用文件，保留数据和备份，删除数据必须单独确认。

## 5. Docker Compose 兼容

保留当前 Compose 入口，不改变 `/submit`、`/admin/`、私有媒体、备份和清空门禁。V2 镜像应：

- 固定 Python 基础镜像摘要和依赖锁定；
- 支持 `docker compose config`、`check`、迁移 dry-run；
- 对 `MAX_STORAGE_MB`、请求上限、Gunicorn 超时和 Nginx 上限做启动配置校验；
- 不把 `.env`、数据库、媒体、备份复制进镜像；
- 生产升级前自动要求外部备份路径存在并可写。

## 6. 离线安装和恢复

离线介质包括安装包、依赖清单、SHA256 清单、版本说明、迁移说明和恢复脚本。安装器不得在无网络时尝试下载依赖；缺包时在写入数据前失败。

备份/恢复流程统一使用 V1 已有的 SQLite online backup、媒体 SHA256 manifest 和临时目录替换机制。V2 备份格式增加项目/模板/配置版本清单，但必须能够验证并读取现有 `student-collector-backup-v2`。

## 7. G3 测试门禁

- Windows 11 x64 clean VM：安装、首次初始化、服务重启、端口冲突、无权限目录、断电恢复、升级回滚、卸载保留数据。
- Windows Server x64：服务账户、局域网访问、日志轮转、备份恢复和多进程维护锁。
- Linux clean VM：systemd 启动、非 root 运行、Nginx 代理、升级回滚、离线依赖。
- Docker：现有 NAS Compose 重启、数据持久化、HTTPS 反代、Docker + Nginx 真实 HTTP 上传下载。
- 三种平台导入同一虚构数据，数据库、媒体、SHA256、ZIP CRC 和 Excel 数值统计一致。

