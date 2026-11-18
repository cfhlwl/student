# Issue #25 — `face_device` 原图兜底验收报告

本报告对应 `codex/face-device-original-fallback-20261118`，基于 `main`
`46752a8aa86140269d7ab23e1557b45c54eff2e4`，仅在本地隔离环境使用合成数据验证。

## 结果

```ini
ISSUE=25
BASE_MAIN_SHA=46752a8aa86140269d7ab23e1557b45c54eff2e4
PRODUCTION_JOB3_CHANGED=NO
PRODUCTION_MEDIA_CHANGED=NO
FALLBACK_ORIGINAL_GATE=PASS_SYNTHETIC
SOURCE_UNAVAILABLE_GATE=PASS_SYNTHETIC
ORIGINAL_BYTES_GATE=PASS_SYNTHETIC
ZIP_CRC_GATE=PASS_SYNTHETIC
PHOTO_ACCOUNTING_GATE=PASS_SYNTHETIC
DOWNLOAD_LIST_ACCOUNTING_GATE=PASS_SYNTHETIC
HISTORICAL_ACCOUNTING_GATE=PASS_UNKNOWN_NOT_ZERO
ORIGINAL_EXPORT_REGRESSION=PASS
WINDOWS_PYTEST=198 passed, 17 warnings
DOCKER_PYTEST=198 passed, 17 warnings
MERGE=HOLD
DEPLOY=NO
PRODUCTION_WRITE=NO
```

## RED → GREEN

在未修改的 `46752a8` 基线 worktree 中运行临时回归：向 `prepare_device_photo` 注入
`jpeg_validation`，源照片已通过大小/SHA256 且可解码，但旧实现没有生成 `待处理原图/`
ZIP 成员，断言失败。该临时测试和 worktree 已删除，未进入提交。

整改后定向测试验证：

- 处理成功的照片进入 `学生照片/`，设备 JPG 计数正确。
- `multiple_faces` 等处理拒绝进入同一目录布局下的 `待处理原图/`，保留原始扩展名和原始字节 SHA256。
- 缺失、损坏或 SHA256 不匹配的源照片不伪造兜底成功，计入源照片不可用和异常原因。
- JPEG、PNG、MPO 的可解码源在设备转换失败时均保留原始扩展名和原始字节。
- ZIP CRC、异常统计表、汇总计数和稳定原因代码一致。
- 下载任务列表显示“实际输出照片 = 设备合格 JPG + 待处理原图”，并同时显示源照片不可用数；历史任务仍显示未知。
- 旧 ExportJob 的新增统计字段保持 `NULL`，状态接口报告“历史统计未知”，不把未知误报为零。

## 测试证据

- `python -m pytest -q tests/test_export_fallback.py`: 9 passed。
- `python manage.py check`: passed。
- `python manage.py makemigrations --check --dry-run`: passed。
- Windows full pytest: 198 passed, 17 warnings。
- Docker Linux isolated full pytest: 198 passed, 17 warnings。
- `git diff --check`: passed。

本轮没有修改人脸检测算法、原始资料导出、生产 Job #3、生产媒体或生产 ZIP；也没有运行生产导出或部署。
