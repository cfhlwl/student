# Student 导出性能优化报告

本报告对应 Issue #24，基于 `46752a8aa86140269d7ab23e1557b45c54eff2e4` 的独立 Worktree。生产侧只做只读采样；合成基准不包含学生姓名、照片或生产数据库。

## 实现边界

- 原始资料导出将照片文件读取、大小/SHA256 校验放入有界 `ThreadPoolExecutor`；ZIP 仍由单一主线程按原顺序写入。
- 默认 `EXPORT_PHOTO_WORKERS=2`、`EXPORT_PHOTO_MAX_INFLIGHT=8`，可通过环境变量调整；设备专用导出保持现有单线程人脸检测和资源信号量，不扩大 OpenCV 并发。
- 学生清单和照片映射在主线程一次性建立，线程只接收文件名、期望大小和 SHA256 等不可变值，不共享 ORM 懒对象。
- 进度内存计数逐学生递增，SQLite 进度更新按最小步长/时间间隔节流，首个 0 和最终总数始终写入；租约、CAS 和最终状态校验未改变。

## 隔离合成基准

样本为 300 张、每张约 64 KiB 的合成 JPEG 字节；每种线程数各运行一次完整原始 ZIP 导出，`ZipFile.testzip()` 通过，并对照片成员名和内容计算相同的内容 SHA256：

| 实现 | 线程 | 总耗时 | CPU 时间 | RSS 变化 | ZIP 照片 | 内容指纹 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 基线 | 1 | 5.4717 s | 1.0781 s | +1.83 MiB | 300 | `862108a5759ed9530555b73eb8e3a3fa42d8dfb253fa5843f4d937645a5f9c5e` |
| 基线 | 2 | 3.3210 s | 1.0312 s | +0.70 MiB | 300 | 同上 |
| 基线 | 4 | 3.0771 s | 0.9688 s | +1.45 MiB | 300 | 同上 |
| 优化 | 1 | 5.5200 s | 1.0469 s | +2.27 MiB | 300 | 同上 |
| 优化 | 2 | 0.3221 s | 0.4844 s | +0.78 MiB | 300 | 同上 |
| 优化 | 4 | 0.3321 s | 0.4844 s | +0.05 MiB | 300 | 同上 |

磁盘 I/O 由 Windows 系统级计数器采样，受缓存和并行测试环境影响，仅作参考；优化 1 线程仍保持原有单线程路径，2/4 线程使用有界内存队列。未把设备模式的 OpenCV CPU 时间混入原始导出结论。

## 生产只读采样

采样时间：`2026-11-18T01:17:23Z`（任务运行中）及 `2026-11-18T01:36:24Z`（任务完成后）。未停止、重启、取消或重入队。

- 任务 3 为 `face_device`，`1075/1075`，attempt 1；从 `01:00:30Z` 至 `01:24:26Z` 完成，约 23 分 56 秒。
- 运行中采样：Export Worker 约 99.96% CPU、435 MiB RSS、Block I/O `318 MB / 2.04 GB`；Web 约 143 MiB，Nginx 约 26 MiB；磁盘 55 GiB 总量、13 GiB 可用（76%）。
- 完成后 queued/running 为 0；三项本次服务容器 restart count 均为 0。
- 完成 ZIP 只读校验：存在，约 48,696,939 bytes，`ZipFile.testzip()` 为 `ok`。聚合成员计数为 172 JPG、84 XLSX；异常统计按类型汇总为 1,806 条“设备照片失败”。未输出任何文件名、姓名或照片内容。

## 验证结果

- `python manage.py check`: PASS
- `python manage.py makemigrations --check --dry-run`: PASS，No changes detected
- Windows targeted: 77 passed, 17 Pillow deprecation warnings
- Windows full: 191 passed, 17 Pillow deprecation warnings
- Docker Linux isolated full: 191 passed, 17 Pillow deprecation warnings
- `git diff --check`: PASS

## 门禁结论

`DIAG_MODE=face_device`

`ACTIVE_JOB=COMPLETED_READ_ONLY`

`BOTTLENECK_EVIDENCE=face_device OpenCV single-slot CPU-bound; original export N+1 photo lookup and per-photo SQLite progress writes removed/throttled in isolated branch`

`THROUGHPUT_GAIN=MEASURED_FOR_ORIGINAL_EXPORT`

`ZIP_CRC_AND_CONTENT=PASS`

`SQLITE_LEASE_LOCK_GATE=PASS`

`PRODUCTION_CHANGE=NO`
