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
    stats = (f"候选 {count} 篇，跳过重复 {report['skipped']} 篇，新评分 {len(report['results'])} 篇，"
             f"失败 {len(failures)} 篇，推荐 {len(selected)} 篇。")
    rule = f"筛选：评分 ≥ {report['min_score']}，最多 {report['top_k']} 篇。仅依据标题和摘要分析。"
    lines = ["# 论文推荐报告", "", stats, "", rule, ""]
    html = ["<!doctype html><html lang='zh-CN'><meta charset='utf-8'><body>",
            "<h1>论文推荐报告</h1>", f"<p>{escape(stats)}</p><p>{escape(rule)}</p>"]
    if not selected:
        lines += ["本次没有达到条件的新推荐论文。", ""]
        html += ["<p>本次没有达到条件的新推荐论文。</p>"]
    for item in selected:
        paper, analysis = item["paper"], item["analysis"]
        url = paper["paper_url"]
        lines += [f"## {paper['paper_title']}", "", url, "", f"评分：{analysis['score']}", "",
                  f"推荐理由：{analysis['reason']}", "", f"摘要：{analysis['summary']}", ""]
        html += [f"<article><h2>{escape(paper['paper_title'])}</h2>", f"<p>评分：{analysis['score']}</p>",
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
    html += ["</body></html>"]
    return "\n".join(lines), "\n".join(html)
