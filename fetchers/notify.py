"""
Tiny email sender for Predicta alerts. Silent no-op when not configured.
Env: SMTP_USER, SMTP_PASSWORD (for Gmail use an App Password, not your normal password),
optional SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 587), ALERT_EMAIL_TO (default SMTP_USER).
"""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("notify")


def email_configured() -> bool:
    return bool(os.environ.get("SMTP_USER") and os.environ.get("SMTP_PASSWORD")) and \
        not os.environ.get("SMTP_PASSWORD", "").startswith("your_")


def send_email(subject: str, body: str) -> dict:
    if not email_configured():
        return {"sent": False, "reason": "email not set up (SMTP_USER / SMTP_PASSWORD missing in .env)"}
    user = os.environ["SMTP_USER"]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ.get("ALERT_EMAIL_TO") or user
    msg.set_content(body)
    try:
        with smtplib.SMTP(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "587")), timeout=20) as s:
            s.starttls()
            s.login(user, os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
        return {"sent": True}
    except Exception as e:
        log.error(f"[NOTIFY] email failed: {type(e).__name__}")
        return {"sent": False, "reason": f"{type(e).__name__}: {str(e)[:120].replace(os.environ.get('SMTP_PASSWORD',''), '***')}"}
