import os
import time
import sqlite3
import requests
from datetime import datetime

# Configuration
APPID = os.environ["STEAM_APPID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
CHECK_MIN = int(os.environ.get("CHECK_EVERY_MINUTES", "360"))
NOTIFY_ON_ZERO_NEW = os.environ.get("NOTIFY_ON_ZERO_NEW", "false").lower() == "true"
DB_PATH = "/data/reviews.db"

URL = f"https://store.steampowered.com/appreviews/{APPID}?json=1&filter=recent&language=all&num_per_page=20"


def init_db():
    """Initialize SQLite database with schema."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        # Create reviews table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS reviews (
                recommendationid TEXT PRIMARY KEY,
                author_steamid TEXT NOT NULL,
                voted_up INTEGER NOT NULL,
                timestamp_created INTEGER NOT NULL,
                timestamp_updated INTEGER NOT NULL,
                review TEXT,
                language TEXT,
                timestamp_fetched INTEGER NOT NULL,
                playtime_forever INTEGER,
                playtime_last_two_weeks INTEGER
            )
        ''')

        # Create indexes for efficient queries
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_voted_up ON reviews(voted_up)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp_fetched ON reviews(timestamp_fetched)')

        conn.commit()
        conn.close()
        print("DB initialized successfully", flush=True)
    except sqlite3.Error as e:
        print(f"ERROR: Database initialization failed: {e}", flush=True)


def get_existing_review_ids(review_ids):
    """Check which review IDs already exist in database."""
    if not review_ids:
        return set()

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        placeholders = ','.join('?' * len(review_ids))
        cursor.execute(
            f'SELECT recommendationid FROM reviews WHERE recommendationid IN ({placeholders})',
            review_ids
        )

        existing = {row[0] for row in cursor.fetchall()}
        conn.close()
        return existing
    except sqlite3.Error as e:
        print(f"ERROR: Failed to check existing reviews: {e}", flush=True)
        return set()


def insert_reviews(new_reviews):
    """Insert new reviews into database."""
    if not new_reviews:
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        timestamp_now = int(time.time())

        for review in new_reviews:
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO reviews (
                        recommendationid, author_steamid, voted_up,
                        timestamp_created, timestamp_updated, review,
                        language, timestamp_fetched, playtime_forever,
                        playtime_last_two_weeks
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    review.get('recommendationid', ''),
                    review.get('author', {}).get('steamid', ''),
                    1 if review.get('voted_up', False) else 0,
                    review.get('timestamp_created', 0),
                    review.get('timestamp_updated', 0),
                    review.get('review', ''),
                    review.get('language', ''),
                    timestamp_now,
                    review.get('author', {}).get('playtime_forever', 0),
                    review.get('author', {}).get('playtime_last_two_weeks', 0)
                ))
            except Exception as e:
                print(f"ERROR: Failed to insert review {review.get('recommendationid')}: {e}", flush=True)

        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"ERROR: Database insert failed: {e}", flush=True)


def get_total_counts():
    """Get total positive/negative review counts from database."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        cursor.execute('SELECT voted_up, COUNT(*) FROM reviews GROUP BY voted_up')
        counts = {row[0]: row[1] for row in cursor.fetchall()}
        conn.close()

        return counts.get(0, 0), counts.get(1, 0)  # negative, positive
    except sqlite3.Error as e:
        print(f"ERROR: Failed to get total counts: {e}", flush=True)
        return 0, 0


def is_first_run():
    """Check if this is the first run (empty database)."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM reviews')
        count = cursor.fetchone()[0]
        conn.close()
        return count == 0
    except sqlite3.Error:
        return True


def fetch_and_process_reviews():
    """Fetch reviews from Steam API and process new ones."""
    try:
        # Fetch reviews from Steam API
        data = requests.get(URL, timeout=30).json()
        reviews = data.get("reviews", [])

        if not reviews:
            print("No reviews fetched from API", flush=True)
            return None

        # Check if first run
        first_run = is_first_run()

        # Extract review IDs and check which are new
        review_ids = [r.get('recommendationid') for r in reviews if r.get('recommendationid')]
        existing_ids = get_existing_review_ids(review_ids)
        new_reviews = [r for r in reviews if r.get('recommendationid') and r.get('recommendationid') not in existing_ids]

        # If no new reviews, return stats with zeros
        if not new_reviews:
            total_neg, total_pos = get_total_counts()
            return {
                'new_positive': 0,
                'new_negative': 0,
                'total_positive': total_pos,
                'total_negative': total_neg,
                'is_first_run': False
            }

        # Insert new reviews
        insert_reviews(new_reviews)

        # Count new reviews
        new_neg = sum(1 for r in new_reviews if not r.get('voted_up', False))
        new_pos = sum(1 for r in new_reviews if r.get('voted_up', False))

        # Get totals from database
        total_neg, total_pos = get_total_counts()

        return {
            'new_positive': new_pos,
            'new_negative': new_neg,
            'total_positive': total_pos,
            'total_negative': total_neg,
            'is_first_run': first_run
        }
    except requests.RequestException as e:
        print(f"ERROR: API request failed: {e}", flush=True)
        return None
    except Exception as e:
        print(f"ERROR: Processing failed: {e}", flush=True)
        return None


def post_notification(stats):
    """Send Discord notification with review stats."""
    if stats is None:
        return

    # Skip notification if no new reviews (unless configured otherwise)
    if stats['new_positive'] == 0 and stats['new_negative'] == 0:
        if not NOTIFY_ON_ZERO_NEW:
            print(f"SKIP No new reviews at {datetime.utcnow().isoformat()}", flush=True)
            return

    # Build message based on first run or subsequent run
    if stats['is_first_run']:
        msg = (
            f"📌 Steam Monitor {APPID} - Initial Run\n"
            f"📝 Loaded {stats['new_positive'] + stats['new_negative']} initial reviews\n"
            f"👎 {stats['new_negative']} / 👍 {stats['new_positive']}\n"
            f"🕒 UTC: {datetime.utcnow():%Y-%m-%d %H:%M}"
        )
    else:
        msg = (
            f"📌 Steam Monitor {APPID}\n"
            f"🆕 NEW: 👎 {stats['new_negative']} / 👍 {stats['new_positive']}\n"
            f"📊 TOTAL: 👎 {stats['total_negative']} / 👍 {stats['total_positive']}\n"
            f"🕒 UTC: {datetime.utcnow():%Y-%m-%d %H:%M}"
        )

    # Send to Discord
    try:
        response = requests.post(WEBHOOK, json={"content": msg}, timeout=30)
        response.raise_for_status()
        print(f"OK Notification sent: new={stats['new_positive']+stats['new_negative']}, total={stats['total_positive']+stats['total_negative']}", flush=True)
    except requests.RequestException as e:
        print(f"ERROR: Discord webhook failed: {e}", flush=True)

# Initialize database
init_db()

# Main monitoring loop
while True:
    try:
        stats = fetch_and_process_reviews()
        post_notification(stats)

        # Log stats
        if stats:
            print(
                f"OK {datetime.utcnow().isoformat()} "
                f"new_neg={stats['new_negative']} new_pos={stats['new_positive']} "
                f"total_neg={stats['total_negative']} total_pos={stats['total_positive']}",
                flush=True
            )
    except Exception as e:
        print(f"ERROR Unexpected error: {repr(e)}", flush=True)

    time.sleep(CHECK_MIN * 60)
