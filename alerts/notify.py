import os
import smtplib
from email.message import EmailMessage


def send_alert_email(ticker: str, label: str, condition: str, threshold: float, price: float) -> bool:
    """
    Send alert notification email. Returns True on success, False if not configured or failed.
    Reads credentials from environment variables — all optional; falls back to console log.

    Required env vars to enable email:
        ALERT_SMTP_HOST, ALERT_SMTP_PORT, ALERT_SMTP_USER, ALERT_SMTP_PASS, ALERT_EMAIL_TO
    """
    host  = os.environ.get("ALERT_SMTP_HOST")
    port  = int(os.environ.get("ALERT_SMTP_PORT", 587))
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
        return False  # email not configured — console log above is the only output

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = user
        msg["To"]      = to
        msg.set_content(body)

        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            smtp.login(user, pwd)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"  email failed: {e}")
        return False
