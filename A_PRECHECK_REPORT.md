# R3-B A 线：上传前资料预检查

## 范围

本线从 R3-A `b80ac31438a6ee44e3544c421273b9d620ef030c` 独立开发，只新增预检查服务和前端资产，未修改现有上传视图、URL、模型、迁移、导出或 R2/R3-A 逻辑。

预检查是提交前提示层，不创建 `UploadSession`，不写入数据库或媒体目录；最终提交服务仍是权威校验边界。

## 实现文件

- `collector/preflight.py`：Excel、照片、匹配和结果摘要的独立服务。
- `templates/collector/submit_preflight.html`：可由集成线挂接的预检查页面。
- `static/js/preflight.js`：文件选择提示、结果渲染、警告确认和阻断按钮控制。
- `tests/test_preflight.py`：虚构名单、照片、损坏文件、限制和匹配场景测试。

## 检查契约

### 服务调用

```python
result = run_preflight(roster_upload, photo_uploads, limits=None)
```

`roster_upload` 和 `photo_uploads` 支持 Django 上传文件或带 `name`、`seek`、`read` 的二进制文件对象。函数会在返回前将输入文件回到起始位置。

`PreflightLimits.from_settings()` 默认读取现有配置：照片数量、单照片大小和总照片大小；测试可传入小额度的 `PreflightLimits`，不需要修改 Django settings。

### 结果

`result.as_dict()` 返回：

- `summary.student_count`：名单内学生数。
- `summary.photo_count`：选中的照片数。
- `summary.matched_count`：可按唯一姓名自动匹配的照片数。
- `summary.missing_count`：没有唯一自动匹配照片的学生数。
- `summary.manual_count`：无法唯一确定、需要人工确认的照片数。
- `blocking_errors`：阻断错误，存在时不得开始正式上传。
- `warnings`：普通警告；无阻断错误时也必须由用户勾选确认后才能继续。
- `records`、`photos`：预检查后的规范化记录和照片状态，含行号、文件名、大小和 SHA256。

每条问题均包含 `code`、`message`、`level`、`row`、`filename`。同名学生不会自动匹配；文件名无法对应学生的照片只进入人工确认，不会随机绑定。

### 建议接线方式（交给 E 线）

1. 在现有 `/submit` 页面提交前调用一个新的管理员/公开入口均可访问的 POST 预检查接口；建议使用 multipart 字段 `roster`、`photos`，并返回 `result.as_dict()`。
2. 若 `blocking_errors` 非空，前端只展示错误并禁止进入现有 `/submit/upload/start/`。
3. 若只有 `warnings`，必须要求用户明确确认后，才调用现有上传开始接口；确认值只作为 UI 流程判断，不替代服务端最终校验。
4. 将 `submit_preflight.html` 的 `preflight_endpoint` 上下文变量设置为新接口 URL，或在集成时把 `data-preflight-endpoint` 指向该 URL。
5. 在正式上传开始前重新提交同一批文件给现有 `parse_roster`、`image_bytes_info`、manifest 和 SHA256 校验路径；预检查结果不应被客户端直接当作可信提交数据。
6. 正式上传成功后，清理预检查页面状态；预检查接口不应返回学生资料检索或文件下载能力。

本线刻意没有修改 `collector/views.py`、`collector/urls.py`、`static/js/app.js` 和现有管理员模板，因此当前主 `/submit` 尚未自动展示预检查页面；这是预留给 E 线解决共享接线冲突的明确未完成点。

## RED → GREEN 证据

### RED

- 本地 `python -m pytest -q tests/test_preflight.py`：退出码 `1`，原因是宿主 Python 环境缺少 `openpyxl`，测试收集失败。
- Docker 依赖环境首次执行：`2 failed, 3 passed`，退出码 `1`；失败暴露了测试期望与实现需要调整的摘要/混合班级场景。

### GREEN

- Docker 隔离环境定向测试：`5 passed`，退出码 `0`。
- Docker 隔离环境全量回归：`69 passed in 21.79s`，退出码 `0`。
- `python -m py_compile collector/preflight.py`：退出码 `0`。
- `node --check static/js/preflight.js`：退出码 `0`。
- `git diff --check`：退出码 `0`。
- 当前 Docker 镜像构建：退出码 `0`。

测试仅使用虚构学生和隔离 Docker 容器，未访问生产服务器，未触发 GitHub Actions，未合并或部署。

## 未完成接线点

- 尚未增加预检查 URL 或修改现有提交页面；按 A 线文件所有权要求留给 E 线集成。
- 因未接入现有 `/submit`，本线不宣称已经完成真实浏览器上传流程的预检查验收。
