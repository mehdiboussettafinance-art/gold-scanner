"""
Simple connectivity test — sends ONE test message to confirm
Telegram + GitHub Secrets are wired up correctly.
This does NOT check the market or send real trading signals.
"""

import os
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

message = (
    "TEST MESSAGE — Gold Scanner Bot\n"
    "This is a connectivity test, not a real trading signal.\n"
    "If you received this, Telegram + GitHub Secrets are working correctly."
)

url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": message}, timeout=15)

print("Status code:", r.status_code)
print("Response:", r.text)
