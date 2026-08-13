"""Shared Gmail digest sender (the SMTP block used to live in two copies)."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import GMAIL_ADDRESS, GMAIL_APP_PASSWORD


def send_gmail(subject, plain, html):
    """Send a plain+HTML digest to yourself. Returns True on success;
    no-ops with a hint when the app password is unset."""
    if GMAIL_APP_PASSWORD == "YOUR_APP_PASSWORD_HERE":
        print("  [!] Set GMAIL_APP_PASSWORD before emailing.")
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_ADDRESS, GMAIL_ADDRESS
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            srv.sendmail(GMAIL_ADDRESS, GMAIL_ADDRESS, msg.as_string())
        return True
    except smtplib.SMTPAuthenticationError:
        print("  [!] Gmail auth failed - check your App Password.")
    except Exception as e:
        print(f"  [!] Email error: {e}")
    return False
