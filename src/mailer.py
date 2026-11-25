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


class MailConfigurationError(ValueError):
    """A configuration error whose text contains field names, never secrets."""


def email_error_message(error):
    if isinstance(error, MailConfigurationError):
        return str(error)
    if isinstance(error, smtplib.SMTPAuthenticationError):
        return f"SMTP authentication rejected (code {error.smtp_code}); check SMTP authorization code and full login email."
    if isinstance(error, ssl.SSLError):
        return "SMTP TLS failed; check SMTP_SECURITY, SMTP_PORT and local certificate configuration."
    if isinstance(error, (TimeoutError, ConnectionError, smtplib.SMTPConnectError)):
        return "SMTP connection failed; check host, port and network access."
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        return "SMTP rejected recipients; check EMAIL_TO."
    return "SMTP delivery failed; check mailbox settings and inbox before resending."


def build_message(report):
    text, html = render_report(report)
    message = EmailMessage()
    historical = sum(x.get('source') == 'history' for x in report['selected'])
    status = ' [运行异常]' if report['failures'] else ''
    message["Subject"] = (f"论文日报 {report.get('report_date', '')}：新推荐 {len(report['selected']) - historical} / "
                          f"历史回顾 {historical}" + status)
    message["From"] = os.environ["EMAIL_FROM"]
    message["To"] = os.environ["EMAIL_TO"]
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def send_report(report):
    required = ("SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO")
    missing = [key for key in required if not os.getenv(key, "").strip()]
    if missing:
        raise MailConfigurationError("Missing email settings in project .env: " + ", ".join(missing))
    mode = os.getenv("SMTP_SECURITY", "starttls").lower()
    if mode not in ("ssl", "starttls"):
        raise MailConfigurationError("SMTP_SECURITY must be ssl or starttls.")
    try:
        port = int(os.getenv("SMTP_PORT") or ("465" if mode == "ssl" else "587"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        raise MailConfigurationError("SMTP_PORT must be an integer from 1 to 65535.") from None
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
    except (OSError, ValueError, KeyError, smtplib.SMTPException) as exc:
        print("Email failed: " + email_error_message(exc), file=sys.stderr)
        print("Existing report is preserved; no scoring was repeated.", file=sys.stderr)
        return 1
    print("Email sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
