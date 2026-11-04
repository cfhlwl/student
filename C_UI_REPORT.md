# R3-B C 线：UI / 操作体验收尾报告

## 范围

本线基于 `b80ac31438a6ee44e3544c421273b9d620ef030c`，只修改上传成功页、匹配结果页、管理员审核页及其样式/前端辅助脚本。未修改 `collector/services.py`、`collector/views.py`、`collector/urls.py`、模型、迁移或公共上传 `static/js/app.js`。

## 已完成

- 上传成功页增加提交编号、修改凭证、结果链接的复制按钮，并提供复制反馈和凭证保存提示。
- 匹配结果页增加学生总数、已匹配、缺少照片、待人工确认汇总；增加全部/已匹配/缺少照片/待人工确认筛选。
- 管理员补传审核卡片增加班级、学生人数、照片数量、缺漏人数、异常数量、原提交状态和补传状态摘要。
- 管理员侧栏在手机布局保留安全退出入口；危险清空操作通过现有清空 URL 定向样式突出显示，原操作和确认逻辑保留。
- `clipboard.js` 使用浏览器剪贴板 API，HTTP 局域网不可用时提供受控的传统复制回退，并显示失败提示。
- 所有新增控件带键盘焦点、ARIA 状态或可读文本；移动端摘要、按钮和筛选器可换行，不依赖横向页面溢出。

## RED → GREEN

- 基线临时树（R3-A HEAD）执行 `tests/test_ui_r3_b.py`：4 failed，退出码 `1`。
  - 缺少复制控件、汇总筛选、审核前摘要和 `clipboard.js`。
- 当前实现隔离 Docker 执行同一测试：`4 passed`，退出码 `0`。

## 自动化测试

| 检查 | 结果 | 退出码 |
|---|---:|---:|
| UI 专项 `python -m pytest -q tests/test_ui_r3_b.py`（Docker） | 4 passed | 0 |
| 全量 `python -m pytest -q`（Docker） | 68 passed | 0 |
| `node --check static/js/clipboard.js` | passed | 0 |
| `git diff --check` | passed | 0 |

## Playwright 证据

隔离 Docker `runserver` 地址：`http://127.0.0.1:18085`。

- 桌面匹配结果页：`output/playwright/r3b-result-desktop.png`
  - 汇总数字可见。
  - 四种状态筛选按钮可见；点击“已匹配”及“缺少照片”可更新显示。
- 手机匹配结果页（390×844）：`output/playwright/r3b-result-mobile.png`
  - 摘要卡片两列排列，筛选器换行，操作按钮未被遮挡。
- 桌面管理员概览：`output/playwright/r3b-admin-desktop.png`
  - “清空全部采集数据”按钮突出显示，原有确认动作保留。
  - 管理员“安全退出”可见。
- 手机管理员审核页（390×844）：`output/playwright/r3b-admin-mobile.png`
  - 顶部“退出”和侧栏“安全退出”均可见。
  - 补传审核卡片、班级信息和“审核前摘要”可见。

## E 线接线需求

当前 UI 严格使用现有上下文：结果页从 `students` 在浏览器端计算汇总，管理员补传摘要从 `supplement.submission.students` 和 `supplement.photos` 的现有关系计算。若 E 线要求服务端权威数字或服务端筛选，应在后端提供明确的 `matched_count`、`missing_count`、`ambiguous_count` 和每个补传的 `missing_count`/`exception_count` 上下文后再替换前端计算；本线未改后端，避免与 A/B/D 线冲突。

## 门禁

- 已提交独立 commit，推送至 `student` 远程同名分支 `codex/r3-b-ui-final`。
- 未创建或修改 main，未合并，未部署，未触发 GitHub Actions，未操作生产环境。
