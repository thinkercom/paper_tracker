"""Send an existing report; never re-score papers while retrying delivery."""

import argparse
import json
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path

from .dify_client import load_config
from .reporting import render_report


def build_message(report):
    text, html = render_report(report)
    message = EmailMessage()
    message["Subject"] = f"论文推荐报告：{len(report['selected'])} 篇推荐 / {len(report['failures'])} 篇失败"
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def send_report(report):
    required = ("SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO")
    if any(not os.getenv(key, "").strip() for key in required):
        raise ValueError("Email configuration incomplete; set SMTP_SERVER/USERNAME/PASSWORD and EMAIL_FROM/TO.")
    mode = os.getenv("SMTP_SECURITY", "starttls").lower()
    if mode not in ("ssl", "starttls"):
        raise ValueError("SMTP_SECURITY must be ssl or starttls.")
    port = int(os.getenv("SMTP_PORT") or ("465" if mode == "ssl" else "587"))
    context = ssl.create_default_context()
    server_type = smtplib.SMTP_SSL if mode == "ssl" else smtplib.SMTP
    options = {"context": context} if mode == "ssl" else {}
    with server_type(os.environ["SMTP_SERVER"], port, timeout=30, **options) as server:
        if mode == "starttls":
            server.starttls(context=context)
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        refused = server.send_message(build_message(report))
        if refused:
            raise ValueError("Some email recipients were refused; inspect mailbox configuration before resending.")


def main():
    load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        send_report(json.loads(args.report.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, smtplib.SMTPException):
        print("Email failed; check SMTP configuration. Existing report is preserved; no scoring was repeated.", file=sys.stderr)
        return 1
    print("Email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
