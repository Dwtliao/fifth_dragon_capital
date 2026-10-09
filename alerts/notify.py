import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    error: str | None = None


def email_configured():
    return all(os.environ.get(k) for k in
               ('ALERT_SMTP_HOST', 'ALERT_SMTP_USER', 'ALERT_SMTP_PASS', 'ALERT_EMAIL_TO'))


def deliver_alert_email(ticker: str, label: str, condition: str, threshold: float, price: float) -> DeliveryResult:
    """
    Send email and return a distinct sent/not_configured/failed result.
    Reads credentials from environment variables — all optional; falls back to console log.

    Required env vars to enable email:
        ALERT_SMTP_HOST, ALERT_SMTP_PORT, ALERT_SMTP_USER, ALERT_SMTP_PASS, ALERT_EMAIL_TO
    """
    host  = os.environ.get("ALERT_SMTP_HOST")
    user  = os.environ.get("ALERT_SMTP_USER")
    pwd   = os.environ.get("ALERT_SMTP_PASS")
    to    = os.environ.get("ALERT_EMAIL_TO")

    name  = label or ticker
    sign  = ">" if condition == "above" else "<"
    subject = f"[FDC Alert] {name}: {ticker} {sign} {threshold:,.2f}"
    body = (
        f"Price alert triggered\n\n"
        f"  Ticker    : {ticker}\n"
        f"  Label     : {name}\n"
        f"  Condition : {ticker} {sign} {threshold:,.2f}\n"
        f"  Last price: {price:,.4f}\n"
    )

    print(f"ALERT: {subject}  (price={price:,.4f})")

    if not all([host, user, pwd, to]):
        return DeliveryResult('not_configured', 'Email configuration is incomplete')

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = to
        msg.set_content(body)

        port = int(os.environ.get("ALERT_SMTP_PORT", 587))
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls()
            smtp.login(user, pwd)
            smtp.send_message(msg)
        return DeliveryResult('sent')
    except Exception as e:
        print(f"  email failed ({type(e).__name__})")
        # Do not persist credentials or raw server messages that may contain secrets.
        return DeliveryResult('failed', f'SMTP delivery failed ({type(e).__name__})')


def send_alert_email(ticker: str, label: str, condition: str, threshold: float, price: float) -> bool:
    """Compatibility wrapper for callers requiring a boolean."""
    return deliver_alert_email(ticker, label, condition, threshold, price).status == 'sent'
