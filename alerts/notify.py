"""Alert email is disabled by operator policy; notifications belong in P11."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    error: str | None = None


def email_configured():
    return False


def deliver_alert_email(ticker: str, label: str, condition: str, threshold: float, price: float) -> DeliveryResult:
    """Compatibility entrypoint: never contacts SMTP, even with saved credentials."""
    return DeliveryResult('not_configured', 'Alert email is disabled; review alerts on P11')


def send_alert_email(ticker: str, label: str, condition: str, threshold: float, price: float) -> bool:
    """Compatibility wrapper; alert email is disabled."""
    return False
