# Changelog

## v1.0.0 · 2026-11-20

首个正式版本：arXiv 论文采集 → Dify 相关性评分 → Python 筛选与去重 → SMTP 日报 → GitHub Actions 定时执行。

### 功能

- 标题与摘要驱动的四字段评分接口，兼容 DeepSeek text、推理前缀和 JSON 代码围栏。
- 持久化论文去重、研究兴趣/分析版本隔离、最低分和 Top K 筛选。
- arXiv 元数据缓存、串行访问、Retry-After 冷却与有限重试。
- JSON、Markdown、HTML 报告及纯文本/HTML 邮件；支持独立重发已有报告。
- GitHub Actions 每日北京时间 08:00 运行、状态提交与报告 artifact。
- 完整中文 README、Dify 配置说明与用户提供的编排截图。

### 验证与边界

- 22 项离线测试；真实 Dify API、arXiv 抓取、本地报告和 GitHub Actions 已分别验证。
- 评分仅基于标题与摘要；服务限流和调度延迟可能影响当次运行。
- [云端完整验收](https://github.com/thinkercom/paper_tracker/actions/runs/44702107411) 成功：22 项测试通过，历史论文去重 5 篇，失败 0 篇，SMTP 返回 `Email sent.`，报告 artifact 上传成功。
