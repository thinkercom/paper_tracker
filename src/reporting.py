"""Render already-scored papers, without triggering more model calls."""

from html import escape
from urllib.parse import urlsplit


def select_results(results, threshold, top_k):
    return sorted((x for x in results if x["analysis"]["score"] >= threshold),
                  key=lambda x: x["analysis"]["score"], reverse=True)[:top_k]


def render_report(report):
    count = report["count"]
    failures = report["failures"]
    selected = report["selected"]
    historical = sum(x.get('source') == 'history' for x in selected)
    stats = (f"候选 {count} 篇，跳过重复 {report['skipped']} 篇，新评分 {len(report['results'])} 篇，"
             f"失败事件 {len(failures)} 项，新推荐 {len(selected) - historical} 篇，历史回顾 {historical} 篇。")
    rule = f"筛选：评分 ≥ {report['min_score']}，最多 {report['top_k']} 篇。仅依据标题和摘要分析。"
    lines = ["# 论文推荐报告 " + report.get('report_date', ''), "", stats, "", rule, ""]
    html = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'><body>",
            "<h1>论文推荐报告</h1>", f"<p>{escape(stats)}</p><p>{escape(rule)}</p>"]
    if historical:
        notice = '本次包含已评分历史回顾，不是今日新抓取的论文。历史记录不足时可能重复回顾；抓取或评分异常详见下方。'
        lines += [notice, '']
        html += [f'<p><strong>{escape(notice)}</strong></p>']
    if not selected:
        lines += ["本次没有达到条件的新推荐论文。", ""]
        html += ["<p>本次没有达到条件的新推荐论文。</p>"]
    for item in selected:
        paper, analysis = item["paper"], item["analysis"]
        url = paper["paper_url"]
        source = '历史回顾（非今日新论文）' if item.get('source') == 'history' else '新评分候选（不代表当天发表）'
        metadata = f"类型：{source}；原始发布日期：{paper.get('published') or '未知，请查看原文'}"
        lines += [f"## {paper['paper_title']}", "", metadata, '', url, "", f"评分：{analysis['score']}", "",
                  f"推荐理由：{analysis['reason']}", "", f"摘要：{analysis['summary']}", ""]
        html += [f"<article><h2>{escape(paper['paper_title'])}</h2>", f'<p>{escape(metadata)}</p>', f"<p>评分：{analysis['score']}</p>",
                 f"<p>{escape(analysis['reason'])}</p><p>{escape(analysis['summary'])}</p>"]
        if urlsplit(url).scheme in ("http", "https"):
            html += [f'<p><a href="{escape(url, quote=True)}">论文原文</a></p>']
        html += ["</article><hr>"]
    if failures:
        html += ["<h2>本次失败</h2><ul>"]
    for failure in failures:
        text = f"{failure['paper_url']} — {failure['error']}"
        lines += ["失败：" + text, ""]
        html += [f"<li>{escape(text)}</li>"]
    if failures:
        html += ["</ul>"]
    if report.get('workflow_url'):
        url = report['workflow_url']
        lines += ['运行详情：' + url, '']
        if urlsplit(url).scheme in ('http', 'https'):
            html += [f'<p><a href="{escape(url, quote=True)}">查看本次运行详情</a></p>']
    html += ["</body></html>"]
    return "\n".join(lines), "\n".join(html)
