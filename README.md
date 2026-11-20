# 论文推荐：arXiv + Dify

工作目录：`D:\03_Project\Agent_article_recomand`。参考项目 `D:\03_Project\arxiv_paper_tracker` 保持原样。

## 已完成与待验证

- Dify text 输出已通过真实 API 验证，兼容开头完整的 `<think>...</think>` 和 JSON 代码围栏，随后严格校验四字段 JSON。不会自动补齐截断 JSON。
- 单篇 `analyze_paper()` 生成本地报告、真实 arXiv 抓取到 Dify 评分的链路均已成功。
- 已实现持久化去重、最低评分、Top K、有限重试和邮件报告。14 项离线测试通过；实际连续运行验证了第二次跳过重复论文。
- Actions 配置已生成，YAML 已解析验证；当前目录尚未关联 GitHub 仓库，尚未在 GitHub 执行。
- SMTP 尚未配置；邮件格式和调用通过模拟测试，尚未验证真实投递。

## 安装与本地运行

```powershell
Set-Location D:\03_Project\Agent_article_recomand
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 仅首次配置时复制；已有 .env 时直接编辑，勿覆盖。
Copy-Item .env.example .env
notepad .env
```

`.env` 使用 Workflow 应用 API Key，API 根地址包含 `/v1`，不含 `/workflows/run`。现有 `.env` 已保留，新选项都有默认值。

```powershell
# 单篇真实 API 诊断：每次都会调用，便于验证 Dify。
python -m src.smoke_test
# 正常流程：去重、筛选和本地报告。
python -m src.main --paper-file examples/test_paper.json
# 真实抓取：默认最近 5 天，每次先少量调用。
python -m src.main --max-papers 1
# 修改最终筛选规则。
python -m src.main --min-score 75 --top-k 5
# 明确重新评分历史论文，会再次产生模型用量。
python -m src.main --paper-file examples/test_paper.json --force
```

如果使用虚拟环境，把上述 `python` 换成 `.\.venv\Scripts\python.exe`。无需激活脚本。
报告存于 `reports/`：JSON 保存所有本次成功结果、推荐列表与失败；Markdown 和 HTML 展示筛选后的推荐与失败统计。没有新推荐也会说明原因。

## 规则与状态

- `MIN_SCORE=70`，`TOP_K=5`；最终筛选由 Python 评分规则决定，模型 `recommended` 只作参考。
- 以不含版本号的 arXiv ID 和研究兴趣、`ANALYSIS_PROFILE` 组合去重。同批重复和历史成功均跳过，失败论文不写入完成状态。
- `data/processed.json` 保存成功记录、论文元数据和评分，逐篇原子写入。更换兴趣会重新评分；更改 Dify 提示词/模型后，可增加 `ANALYSIS_PROFILE` 版本触发重新分析。
- 修改阈值不会自动重推已处理的历史论文；历史分析仍保存在状态文件和旧报告中。
- 同目录同时运行时，第二个进程会被 `data/run.lock` 拒绝。异常终止留下锁时，先确认无进程运行再移除该锁。
- `DIFY_MAX_RETRIES=2` 表示首次调用外最多重试 2 次，范围 0–5。仅对 HTTP 429 和连接建立超时重试，指数退避；支持数字秒数的 Retry-After（等待上限 60 秒）。
- 读取超时、5xx、无效 JSON 和 Workflow 失败不自动重发，因为远端可能已执行并计费。先检查 Dify 日志再人工重跑。
- 非法字段、未闭合推理标签、截断 JSON 会报错，不将错误结果当成成功论文。

## 邮件

先填 `.env.example` 中的 SMTP 与邮箱字段到本地 `.env`。支持 `SMTP_SECURITY=starttls`（默认端口 587）或 `ssl`（常用端口 465）；以邮箱服务商要求为准。

```powershell
python -m src.main --max-papers 1 --send-email
# 仅重发已生成的报告，不再调用模型，也不重新抓取。
python -m src.mailer --report reports/papers-具体时间.json
```

未指定 `--send-email` 不发送。邮件提供纯文本与 HTML 两种内容，转义论文标题和模型文本，包含失败统计。投递失败返回非零退出码并保留报告；不自动重发邮件，避免 SMTP 已接受邮件但响应丢失时重复投递。

## GitHub Actions

文件：`.github/workflows/daily_paper_analysis.yml`。

1. 将项目放入目标 GitHub 仓库，提交源码、依赖、工作流；不要提交 `.env`。不必复制参考项目的 DeepSeek 工作流。
2. 在 Settings → Secrets and variables → Actions 添加 Repository secrets：`DIFY_API_KEY` 和 `DIFY_API_URL`（包含 `/v1`）。本地 `.env` 不会自动传到 GitHub。
3. 可设置 Repository variables：`RESEARCH_INTERESTS`、`ARXIV_CATEGORIES`、`MAX_PAPERS`、`MIN_SCORE`、`TOP_K`、`ANALYSIS_PROFILE`。默认每次最多抓取 3 篇、评分至少 70、最多推荐 5 篇。
4. 确保 Actions 可写仓库内容，且分支保护允许机器人提交状态。默认工作流仅提交 `data/processed.json`，不会 `git add .`；该文件含论文信息、研究兴趣及评分，仓库可见者也可见这些记录。
5. 在 Actions → Daily Paper Analysis → Run workflow 手动运行。查看评分步骤和 artifact 中报告，确认持久化步骤也成功。
6. 首次云端手动运行成功后，设置 Repository variable `ENABLE_SCHEDULE=true`。计划为北京时间每天 08:00（UTC 00:00）；GitHub 计划任务可能延迟，不保证整点开始。
7. 邮件默认关闭。需要时再配置 SMTP/邮箱 Secrets 和 `SMTP_SECURITY` variable，设置 `SEND_EMAIL=true`。SMTP_PORT 使用 Secret，465 配合 ssl、587 配合 starttls。

工作流将报告和恢复状态保存为 artifact，保留 30 天。即便部分评分或邮件失败，也尝试保存成功评分状态。若状态提交被权限/分支保护拒绝，工作流会失败；可从 artifact 恢复状态，避免下次重复调用。不要将临时磁盘或可能被清理的 cache 当成唯一去重存储。

GitHub 托管 Runner 必须能访问 Dify；本机 localhost 无法直接访问。当前尚未连接目标仓库，未执行上传、提交、推送或云端运行。

## 验证与 Dify 配置

```powershell
python -m unittest discover -s tests -v
```

Dify 原始搭建步骤见 `docs/DIFY_SETUP.md`。输出节点变量名仍为 `result`，引用 LLM 的 `text` 即可。若界面支持“推理标签分离”，可开启后重新发布，以减少推理段混入 text；Python 也会兼容现有格式。

官方资料：
- [Dify LLM 与推理分离](https://docs.dify.ai/en/cloud/use-dify/nodes/llm)
- [Dify Workflow API](https://docs.dify.ai/en/api-reference/workflow-runs/run-workflow)
- [GitHub Python 工作流](https://docs.github.com/en/actions/tutorials/build-and-test-code/python)
- [GitHub 计划触发](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
