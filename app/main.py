import os
import time
import sqlite3
import re
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# Configuration
APPID = os.environ["STEAM_APPID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
CHECK_MIN = int(os.environ.get("CHECK_EVERY_MINUTES", "360"))
NOTIFY_ON_ZERO_NEW = os.environ.get("NOTIFY_ON_ZERO_NEW", "false").lower() == "true"
DB_PATH = "/data/reviews.db"

URL = f"https://store.steampowered.com/appreviews/{APPID}?json=1&filter=recent&language=all&num_per_page=20"


# Helper functions for discussion scraping
def parse_steam_timestamp(text):
    """Parse Steam's timestamp format to Unix timestamp."""
    # Steam uses various formats: "2 hours ago", "Jan 15 @ 3:45pm", etc.
    # For MVP: return current time as fallback
    # TODO: Implement full timestamp parser if needed
    try:
        # Basic parsing - for now just return current time
        return int(time.time())
    except:
        return int(time.time())


def extract_number(element, keyword):
    """Extract number from text containing keyword (e.g., '15 replies')."""
    try:
        text = element.text if hasattr(element, 'text') else str(element)
        match = re.search(r'(\d+)\s*' + keyword, text, re.IGNORECASE)
        return int(match.group(1)) if match else 0
    except:
        return 0


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

        # Create discussions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS discussions (
                gid_discussion TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                author_steamid TEXT NOT NULL,
                author_name TEXT,
                timestamp_created INTEGER NOT NULL,
                content_snippet TEXT,
                reply_count INTEGER DEFAULT 0,
                view_count INTEGER DEFAULT 0,
                is_pinned INTEGER DEFAULT 0,
                timestamp_fetched INTEGER NOT NULL
            )
        ''')

        # Create indexes for discussions
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp_created ON discussions(timestamp_created)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_timestamp_fetched_disc ON discussions(timestamp_fetched)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_is_pinned ON discussions(is_pinned)')

        conn.commit()
        conn.close()
        print("DB initialized successfully (reviews + discussions)", flush=True)
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


# Discussion monitoring functions
def fetch_discussions():
    """Fetch and parse discussions from Steam community forums."""
    url = f"https://steamcommunity.com/app/{APPID}/discussions/0/"

    try:
        # Add headers to mimic browser request
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        # Parse HTML with BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')

        discussions = []
        # Find discussion containers - NOTE: These selectors may need adjustment after testing
        # Steam forums structure: looking for discussion topics
        topic_containers = soup.find_all('div', class_='forum_topic')

        if not topic_containers:
            print("WARNING: No discussion containers found - HTML structure may have changed", flush=True)
            return []

        for topic in topic_containers[:20]:  # Limit to first 20 discussions
            try:
                # Extract discussion link and gid
                link = topic.find('a', class_='forum_topic_name')
                if not link:
                    continue

                href = link.get('href', '')
                title = link.text.strip()

                # Extract gid from URL pattern: .../discussions/0/GIDHERE/
                gid_match = re.search(r'/discussions/\d+/(\d+)/', href)
                if not gid_match:
                    continue

                gid = gid_match.group(1)

                # Extract author information
                author_link = topic.find('a', class_='forum_topic_author')
                author_name = author_link.text.strip() if author_link else 'Unknown'
                author_steamid = ''  # May need to extract from profile link if available

                # Timestamp (using current time as fallback per plan)
                timestamp = int(time.time())

                # Reply/view counts
                stats_div = topic.find('div', class_='forum_topic_stats')
                reply_count = extract_number(stats_div, 'replies?') if stats_div else 0
                view_count = 0  # Views may not be available in list view

                # Check if pinned
                is_pinned = 1 if topic.find(class_='forum_topic_pinned') or 'sticky' in topic.get('class', []) else 0

                # Content snippet from preview
                preview_div = topic.find('div', class_='forum_topic_preview')
                content_snippet = preview_div.text.strip()[:200] if preview_div else ''

                discussions.append({
                    'gid_discussion': gid,
                    'title': title,
                    'author_steamid': author_steamid,
                    'author_name': author_name,
                    'timestamp_created': timestamp,
                    'content_snippet': content_snippet,
                    'reply_count': reply_count,
                    'view_count': view_count,
                    'is_pinned': is_pinned
                })

            except Exception as e:
                print(f"ERROR: Failed to parse discussion element: {e}", flush=True)
                continue

        return discussions

    except requests.RequestException as e:
        print(f"ERROR: Failed to fetch discussions: {e}", flush=True)
        return None
    except Exception as e:
        print(f"ERROR: Failed to parse discussions HTML: {e}", flush=True)
        return None


def get_existing_discussion_ids(discussion_ids):
    """Check which discussion IDs already exist in database."""
    if not discussion_ids:
        return set()

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        placeholders = ','.join('?' * len(discussion_ids))
        cursor.execute(
            f'SELECT gid_discussion FROM discussions WHERE gid_discussion IN ({placeholders})',
            discussion_ids
        )

        existing = {row[0] for row in cursor.fetchall()}
        conn.close()
        return existing
    except sqlite3.Error as e:
        print(f"ERROR: Failed to check existing discussions: {e}", flush=True)
        return set()


def insert_discussions(new_discussions):
    """Insert new discussions into database."""
    if not new_discussions:
        return

    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        timestamp_now = int(time.time())

        for disc in new_discussions:
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO discussions (
                        gid_discussion, title, author_steamid, author_name,
                        timestamp_created, content_snippet, reply_count,
                        view_count, is_pinned, timestamp_fetched
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    disc.get('gid_discussion', ''),
                    disc.get('title', ''),
                    disc.get('author_steamid', ''),
                    disc.get('author_name', ''),
                    disc.get('timestamp_created', 0),
                    disc.get('content_snippet', ''),
                    disc.get('reply_count', 0),
                    disc.get('view_count', 0),
                    disc.get('is_pinned', 0),
                    timestamp_now
                ))
            except Exception as e:
                print(f"ERROR: Failed to insert discussion {disc.get('gid_discussion')}: {e}", flush=True)

        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"ERROR: Database insert failed for discussions: {e}", flush=True)


def is_first_run_discussions():
    """Check if this is the first run for discussions (empty table)."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM discussions')
        count = cursor.fetchone()[0]
        conn.close()
        return count == 0
    except sqlite3.Error:
        return True


def fetch_and_process_discussions():
    """Fetch discussions from Steam forums and process new ones."""
    try:
        # Fetch discussions via web scraping
        discussions = fetch_discussions()

        if discussions is None:
            print("Failed to fetch discussions from Steam", flush=True)
            return None

        if not discussions:
            print("No discussions found", flush=True)
            return {'new_count': 0, 'discussions': [], 'is_first_run': False}

        # Check if first run
        first_run = is_first_run_discussions()

        # Extract discussion IDs and check which are new
        discussion_ids = [d.get('gid_discussion') for d in discussions if d.get('gid_discussion')]
        existing_ids = get_existing_discussion_ids(discussion_ids)
        new_discussions = [d for d in discussions if d.get('gid_discussion') and d.get('gid_discussion') not in existing_ids]

        # If no new discussions
        if not new_discussions:
            return {'new_count': 0, 'discussions': [], 'is_first_run': False}

        # Insert new discussions
        insert_discussions(new_discussions)

        # Filter out pinned posts from notifications (they're not "new" content)
        new_discussions_unpinned = [d for d in new_discussions if not d.get('is_pinned')]

        return {
            'new_count': len(new_discussions_unpinned),
            'discussions': new_discussions_unpinned[:10],  # Limit to 10 for notification
            'is_first_run': first_run
        }

    except Exception as e:
        print(f"ERROR: Discussion processing failed: {e}", flush=True)
        return None


def post_discussion_notification(disc_data):
    """Send Discord notification for new discussions."""
    if disc_data is None:
        return

    # Skip if no new discussions (unless configured otherwise)
    if disc_data['new_count'] == 0:
        if not NOTIFY_ON_ZERO_NEW:
            print(f"SKIP No new discussions at {datetime.utcnow().isoformat()}", flush=True)
            return

    # Build message based on first run or subsequent run
    if disc_data['is_first_run']:
        msg = (
            f"📌 Steam Monitor {APPID} - Discussions Initial Run\n"
            f"💬 Loaded {disc_data['new_count']} initial discussions\n"
            f"🕒 UTC: {datetime.utcnow():%Y-%m-%d %H:%M}"
        )
    else:
        # Build detailed list with title and snippet
        msg = f"📌 Steam Monitor {APPID} - New Discussions\n"
        msg += f"💬 {disc_data['new_count']} new discussion(s)\n\n"

        # Add detailed list (max 5 to avoid Discord message limit)
        for i, disc in enumerate(disc_data['discussions'][:5], 1):
            title = disc.get('title', 'Untitled')[:100]  # Truncate long titles
            snippet = disc.get('content_snippet', 'No preview')[:150]
            author = disc.get('author_name', 'Unknown')

            # Create discussion URL
            gid = disc.get('gid_discussion', '')
            url = f"https://steamcommunity.com/app/{APPID}/discussions/0/{gid}/"

            msg += f"{i}. **{title}**\n"
            msg += f"   By: {author}\n"
            if snippet:
                msg += f"   {snippet}...\n"
            msg += f"   {url}\n\n"

        if disc_data['new_count'] > 5:
            msg += f"... and {disc_data['new_count'] - 5} more\n\n"

        msg += f"🕒 UTC: {datetime.utcnow():%Y-%m-%d %H:%M}"

    # Send to Discord
    try:
        response = requests.post(WEBHOOK, json={"content": msg}, timeout=30)
        response.raise_for_status()
        print(f"OK Discussion notification sent: new={disc_data['new_count']}", flush=True)
    except requests.RequestException as e:
        print(f"ERROR: Discord webhook failed for discussions: {e}", flush=True)


# Initialize database
init_db()

# Main monitoring loop
while True:
    try:
        # Process reviews
        stats = fetch_and_process_reviews()
        post_notification(stats)

        # Log review stats
        if stats:
            print(
                f"OK {datetime.utcnow().isoformat()} "
                f"new_neg={stats['new_negative']} new_pos={stats['new_positive']} "
                f"total_neg={stats['total_negative']} total_pos={stats['total_positive']}",
                flush=True
            )

        # Add delay between reviews and discussions to respect rate limits
        time.sleep(5)

        # Process discussions
        disc_data = fetch_and_process_discussions()
        post_discussion_notification(disc_data)

        # Log discussion stats
        if disc_data:
            print(
                f"OK {datetime.utcnow().isoformat()} "
                f"new_discussions={disc_data['new_count']}",
                flush=True
            )

    except Exception as e:
        print(f"ERROR Unexpected error: {repr(e)}", flush=True)

    time.sleep(CHECK_MIN * 60)
