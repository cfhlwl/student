# V1 测试及自审报告

日期：2026-11-03（Asia/Shanghai）

## 当前 Git 证据

- 项目目录：`E:\cfyz\student-collector-v1-r2`
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
| R1 可靠性测试 | `docker compose run --rm -v "${PWD}:/app" web pytest -q -s tests/test_reliability_r1.py` | 0，11 passed；300 张导出 1.045 秒，tracemalloc 峰值 3,759,184 bytes |
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

## R2 RED 复现

- 基线 `6a11f8fc83e3ea7d1909a28452e2e5d073b31bb9` 在独立 R2 worktree 执行 `tests/test_reliability_r2.py`，结果为 `6 failed, 2 passed`，退出码 1。
- 失败项分别复现：总大小超过批次限制时前端不分批、会话未绑定照片清单、Excel/照片变更未被拒绝、重复 `/complete/` 会刷新结果凭证、单班级名单/文档缺失未进入班级统计表、空性别使用女生编号、模板未说明当前在读学年。

## R2 GREEN 证据

| 检查 | 结果 |
|---|---:|
| R2 专项测试 | 0，11 passed；包含 10 班×20 张并发提交、会话清单绑定、重复完成、单班级异常、配额临界、ZIP CRC/字节哈希 |
| 完整自动化回归 | 0，40 passed，99.65 秒 |
| Docker + Nginx 大文件 HTTP | 0，20 张照片、每张至少 2,500,000 bytes，实际最小 3,390,477 bytes，3 批上传；`HTTP_R2_OK photos=20` |
| 大文件导出 | ZIP 约 67,039,168 bytes；CRC、照片 SHA256、Excel 统计逐项核对通过 |
| Django check | 0 |
| 迁移漂移检查 | 0，无变化 |
| R2 隔离环境清理 | 0，容器、网络、测试数据和媒体目录均清理 |

## R2 修复摘要

1. 前端同时按照片数量和照片总字节数决定分批；会话保存照片清单摘要，服务器校验批次名称、大小、顺序和可用 SHA256。
2. Excel 指纹、照片清单和上传凭证绑定；用户改变 Excel、照片或顺序后，旧会话会被拒绝，前端状态也会失效并重新开始。
3. 完成接口使用会话稳定结果凭证，重复完成不会刷新原结果链接。
4. 单班级导出的照片、原始名单和相关文档缺失，都会写入该班级统计表的“异常记录”。
5. 空性别不再套用女生 `02`；默认姓名命名仍保持不变。
6. 模板和公开页面明确“学年”是当前在读学年的起始年份，不是入学年份；走读分类契约固定为走读、半走读、住宿。
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

## R2-FINAL 定点整改（审查评论 ID 6602766641）

本轮基线为 PR #1 的 `0a267ae83caee866668ace633d5b59dbc33759fc`（审查记录 TREE=`bfaa60ea4417c233456db59f424ed4841706a71d`）。未访问或修改生产服务器，未启动 GitHub Actions。

### RED 对照

- R2-F1：基线显式年级尚未贯通到 `ClassGroup`，同一学年不同年级会沿用旧学年映射，审查评论判定为失败。
- R2-F2/F5：基线批次接口允许缺少 `upload_token`，非空照片会话允许空 manifest，审查评论判定为失败。
- R2-F3：基线前端只按照片数量/照片字节分批，Excel、照片和文档混合请求仍可能超过 Nginx 40MB。
- R2-F4：基线最终提交创建数据库记录与完成会话分离，进程中断可能留下半成品或不可恢复状态；重复完成也存在凭证稳定性风险。
- 最终提交第一次真实 Nginx 10 班并发复测中，有 1 个首批照片请求返回 HTTP 500；失败会话仍为 `pending` 且没有批次，日志定位为批次入口未保护的 SQLite 锁竞争读取。该现象作为本轮追加 RED，随后已修复并重新验证。

以上五项均由审查评论 ID `6602766641` 记录为 RED；修复后用例均转为 GREEN。

### 修复与 GREEN 证据

1. R2-F1：模板增加“年级”列；显式年级按初一至高三校验并传入班级归属；旧版无年级列继续使用 2024/2025/2026 映射。新增同一学年六个年级录入、统计和导出路径测试。
2. R2-F2/F5：公开照片/文档批次缺少凭证直接拒绝；非空会话必须提交非空 manifest，并逐批校验名称、大小、顺序和 SHA256。
3. R2-F3：前端将 Excel、照片和文档拆成独立批次，按数量与总字节数共同切分；真实混合请求为 15 张照片 + 20MB 文档，未触发 413。
4. R2-F4：最终化使用 `finalizing` 状态、稳定结果凭证和锁；重复请求返回同一结果；检测并清理半成品提交及其孤儿文件后可重试恢复。
5. 导出继续使用受控临时 ZIP、流式文件复制；缺失照片、名单和文档均写入对应班级 Excel 的“异常记录”；清空全部资料功能和管理员账号保留。
6. 并发入口：去除视图层重复的未保护会话查询；批次、会话创建、公开上传开关读取统一使用有限 SQLite 锁重试，避免并发请求将可恢复锁等待变成 HTTP 500。

| 验收项 | 退出码 / 结果 |
|---|---:|
| Docker 全量自动化回归 | 0，46 passed，38.45 秒 |
| R2-FINAL 定向单元/集成测试 | 0，17 passed，61.55 秒 |
| R2-FINAL 真实 HTTP 首次并发复现 | 1，10 班并发中 1 个批次 HTTP 500 |
| 真实 Nginx 混合上传 + 10 班并发 | 0，15 张照片 + 20MB 文档；10 班×20 张；总导出 215 张；ZIP CRC/字节/SHA256/统计表通过 |
| 既有 Nginx 大文件验收 | 0，20 张照片，每张最小 3,390,477 bytes，3 批；`HTTP_R2_OK` |
| Django check | 0，无问题 |
| makemigrations --check --dry-run | 0，无变化 |
| Compose config | 0 |
| Python 编译检查 | 0 |
| git diff --check | 0 |
| Docker 重启持久化 | 0，重启后读取 `Submission=12, Student=235, StudentPhoto=235` |

真实 HTTP 矩阵还核对了每个并发班级的 20 条学生、20 条照片、名单文件大小和照片 `StudentPhoto.sha256`；混合班级核对了名单、15 条照片和 1 条文档的磁盘内容。全校导出目录按走读、半走读、住宿分层，ZIP 可解压且统计 Excel 数值单元格保持为整数。

## R2-FINAL 最终定点整改（审查评论 ID 6603798119）

本轮只处理 R2-F6、R2-F7，并补做 100 张×约 3MB 的隔离 Docker+Nginx 实际验收；没有修改 R3 事项、生产环境或 GitHub Actions。

### RED 复现

- R2-F6：`finalizing` 会话只要找到同一稳定凭证的 `Submission` 就可返回成功；构造仅有 Submission/Student、无名单/照片记录，以及已有部分照片的半成品，均可能虚报完成。另构造完成标记异常和双 `/complete/` 竞争场景。
- R2-F7：标准库最小复现证明在 ZIP `archive.open(..., "w")` 内读源文件中途失败时，关闭 ZIP 仍可能留下 CRC 合法但内容截断的成员；原实现只写异常表，未回滚该成员。

### 修复

1. `UploadSession` 增加 `expected_student_count` 并迁移；最终化恢复只在名单、学生数、照片数、文档数、物理文件、照片大小/SHA256、学生关联和 `public_submit` 审计记录全部完整时才标记 `completed`。新鲜的 `finalizing` 半成品只返回可重试错误，过期后才允许安全回到 `pending`，重试仍保持稳定结果凭证且只生成一个 Submission。清理半成品时保护仍被上传会话引用的 staged 文件，避免恢复时误删输入。
2. 导出照片先复制到受控临时文件并校验大小/SHA256，再写入 ZIP；源读取失败只进入异常工作表，不产生照片成员；ZIP 写入本身发生 I/O 错误则整包删除，不把截断成员作为成功结果。成功计数仍来自实际写入的完整照片。

### RED→GREEN 测试证据

| 验收项 | 退出码 / 结果 |
|---|---:|
| F6/F7 定向测试（含 finalizing 无照片、finalizing 部分照片、过期恢复、双 complete、导出源读失败） | 0，5 passed，约 1.62 秒 |
| Docker 全量自动化回归 | 0，51 passed，37.46 秒 |
| Docker + Nginx 原有 R2 真实矩阵 | 0，`HTTP_R2_FINAL_OK mixed=15+20MB-doc concurrent_classes=10 concurrent_photos=200 total_photos=215 zip_bytes=50687707` |
| 100×约3MB 单会话 Docker + Nginx | 0，`HTTP_R2_100X3_OK photos=100 batches=12 db_photos=100 media_photo_files=100 zip_photos=100 zip_bytes=335109954 elapsed_seconds=85.43 client_maxrss_kb=870940 media_bytes=339126137` |
| 100×3MB 失败批次与重复请求 | 0，批次 1 的错误凭证请求返回 400；随后同批有效重试和重复提交均成功且无重复记录 |
| 100×3MB ZIP 完整性 | 0，100 张照片逐张字节/SHA256/CRC 校验；三类目录与分类统计表合计人数、成功照片数、缺失数分别为 100、100、0 |
| 100×3MB 管理员审核 | 0，审核后唯一采用提交，数据库照片记录 100 条 |
| 100×3MB 私有磁盘 | 0，最终照片目录恰好 100 个文件；媒体目录占用 339,126,137 字节 |

100×3MB 测试的 JPEG 实际每张约 2.8–4.2MB，均为不同可校验内容；总响应 ZIP 约 335MB。测试期间 Docker 观测到 web 容器约 526–807MiB（测试客户端与 Gunicorn 同容器，`client_maxrss` 870,940KB；导出完成后服务容器回落到约 108MiB），Nginx 使用临时文件缓冲大响应，未出现 413/500。测试数据随后已从隔离环境清理。

### 本轮最终自审

- 保留管理员“清空全部采集资料”功能及数据库/媒体配套删除逻辑。
- 未修改现有 EasyPrinter、cfyz 生产服务或生产数据库；未部署、未合并、未触发 GitHub Actions。
- 当前仍等待 ChatGPT 独立复审，`MERGE=HOLD_FOR_CHATGPT`、`DEPLOY=NO`、`R3=WAIT_R2_FINAL_PASS`。

