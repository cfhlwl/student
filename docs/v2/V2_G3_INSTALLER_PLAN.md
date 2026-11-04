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

升级采用 `releases/<version>` + `current` 原子指针，但程序目录回退不等于数据库回滚。完整升级/恢复契约见 [`V2_D1_D5_CONTRACTS.md`](V2_D1_D5_CONTRACTS.md)。卸载默认停止服务并删除应用文件，保留数据和备份，删除数据必须单独确认。

### 4.1 升级与失败恢复时序

1. 写入持久化维护状态 `upgrading`，取得跨进程文件锁，阻断新上传、补传、审核、人工匹配和配置写入。
2. 等待已有写事务排空；超时则安全停机，不执行迁移。
3. 在异机或独立挂载路径创建整套 SQLite online backup、`media/`、模板/项目配置、安装配置和版本清单；逐项 SHA256、大小、ZIP CRC 验证，备份文件不可覆盖。
4. 停止 Windows Service、systemd unit 或 Docker web/nginx，并确认所有 worker/子进程退出。
5. 执行 `migrate --plan`，再执行只支持向前的迁移；迁移中断不得依赖旧程序自动继续。
6. 启动新版本，执行健康端点、数据库读写、模板版本、媒体 manifest 和导出最小样例检查。
7. 成功后保持公开采集 `closed`，管理员确认检查结果后再开启；升级过程不自动重新开放。

### 4.2 回滚矩阵和退出码

| 状态 | 允许动作 | 结果 |
| --- | --- | --- |
| 预检查失败 | 不停服务或安全停止 | 退出 10，数据不变 |
| 备份/manifest 验证失败 | 不迁移 | 退出 20，维护状态恢复为关闭/原状态 |
| 迁移失败 | 停机并用升级前整套备份恢复 | 退出 30；恢复 SQLite、媒体、配置后才能启动旧版本 |
| 新版本健康检查失败 | 不直接启动旧版本；先恢复升级前整套备份 | 退出 30 或 50，gate 保持关闭 |
| 恢复失败、哈希不一致或进程中断 | 保留现场和备份，进入 `recovery_required` | 退出 40，所有写入拒绝，等待人工处理 |
| 全部检查通过 | 新版本可启动 | 退出 0，公开采集仍需明确开启 |

Windows 和 Linux 都必须演练：迁移前中断、迁移中断、媒体复制中断、服务启动失败、恢复中断、重启后状态文件仍为 `upgrading/recovery_required`。旧程序绝不能连接已迁移但未恢复的 SQLite。

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
- Docker：现有 NAS Compose 重启、数据持久化、HTTPS 反代、Docker + Nginx 真实 HTTP 上传下载；升级前必须显式 stop/drain/backup，禁止以容器启动时自动迁移冒充可回滚升级。
- 三种平台导入同一虚构数据，数据库、媒体、SHA256、ZIP CRC 和 Excel 数值统计一致。
- 失败注入必须核对 SQLite、媒体、配置和 gate 状态四者一致；恢复后默认关闭公开采集。

