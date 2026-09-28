"""
Direct SMTP email sending -- no Aki/Outlook dependency. This is what lets
check_and_alert.py run fully unattended (e.g. on a WorkSpace with nobody
watching), instead of printing alert blocks for a live Aki session to relay.
"""
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import email_config as cfg
except ImportError:
    # email_config.py is a local secrets file (real Gmail App Password) that's
    # gitignored and never committed -- a fresh git clone (e.g. Fly.io's build)
    # won't have it. Fall back to environment variables / Fly secrets instead,
    # so this module works identically either way with zero caller changes.
    import os

    class cfg:
        SENDER_EMAIL = os.environ["ARB_SENDER_EMAIL"]
        SENDER_APP_PASSWORD = os.environ["ARB_SENDER_APP_PASSWORD"]
        RECIPIENT_EMAIL = os.environ["ARB_RECIPIENT_EMAIL"]
        SMTP_HOST = os.environ.get("ARB_SMTP_HOST", "smtp.gmail.com")
        SMTP_PORT = int(os.environ.get("ARB_SMTP_PORT", "465"))


def send_alert_email(subject, html_body):
    """Send one HTML email via Gmail SMTP over SSL (port 465). Returns True on
    success, False on failure (network/auth error) -- never raises, so a mail
    hiccup can't crash the caller's alert loop."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.SENDER_EMAIL
    msg["To"] = cfg.RECIPIENT_EMAIL
    msg.attach(MIMEText(html_body, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.SMTP_HOST, cfg.SMTP_PORT, context=context, timeout=15) as server:
            server.login(cfg.SENDER_EMAIL, cfg.SENDER_APP_PASSWORD)
            server.sendmail(cfg.SENDER_EMAIL, [cfg.RECIPIENT_EMAIL], msg.as_string())
        return True
    except Exception as e:
        print(f"EMAIL SEND FAILED: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    # Quick standalone test -- run this file directly to confirm the credential
    # and network path actually work before wiring it into check_and_alert.py.
    ok = send_alert_email(
        "[TEST] arb-tracker direct SMTP send",
        "<html><body><p>If you're reading this, direct Gmail SMTP sending works "
        "with no Aki/Outlook involved -- this is the mechanism the WorkSpace "
        "version will use to alert fully unattended.</p></body></html>",
    )
    print("SUCCESS" if ok else "FAILED")
