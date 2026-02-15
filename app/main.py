import os, time, requests
from datetime import datetime

APPID = os.environ["STEAM_APPID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
CHECK_MIN = int(os.environ.get("CHECK_EVERY_MINUTES", "360"))

URL = f"https://store.steampowered.com/appreviews/{APPID}?json=1&filter=recent&language=all&num_per_page=20"

def post(msg: str):
    requests.post(WEBHOOK, json={"content": msg}, timeout=30)

while True:
    try:
        data = requests.get(URL, timeout=30).json()
        reviews = data.get("reviews", [])
        neg = sum(1 for r in reviews if not r.get("voted_up", False))
        pos = sum(1 for r in reviews if r.get("voted_up", False))
        msg = f"📌 Steam Monitor {APPID}\n👎 {neg} / 👍 {pos}\nUTC: {datetime.utcnow():%Y-%m-%d %H:%M}"
        post(msg)
        print("OK", datetime.utcnow().isoformat(), "neg", neg, "pos", pos, flush=True)
    except Exception as e:
        print("ERROR", repr(e), flush=True)
    time.sleep(CHECK_MIN * 60)
