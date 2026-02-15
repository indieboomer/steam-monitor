import os
import time
import sqlite3
import re
import requests
from datetime import datetime
from bs4 import BeautifulSoup
from openai import OpenAI

# Configuration
APPID = os.environ["STEAM_APPID"]
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
CHECK_MIN = int(os.environ.get("CHECK_EVERY_MINUTES", "360"))
NOTIFY_ON_ZERO_NEW = os.environ.get("NOTIFY_ON_ZERO_NEW", "false").lower() == "true"
DB_PATH = "/data/reviews.db"

URL = f"https://store.steampowered.com/appreviews/{APPID}?json=1&filter=recent&language=all&num_per_page=20"

# Cache for game name
GAME_NAME_CACHE = None

# AI Summary Configuration
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
ENABLE_AI_SUMMARY = os.environ.get("ENABLE_AI_SUMMARY", "false").lower() == "true"
AI_SUMMARY_INTERVAL_HOURS = int(os.environ.get("AI_SUMMARY_INTERVAL_HOURS", "24"))
AI_SUMMARY_DAYS_LOOKBACK = int(os.environ.get("AI_SUMMARY_DAYS_LOOKBACK", "7"))
AI_SUMMARY_MODEL = os.environ.get("AI_SUMMARY_MODEL", "gpt-4o-mini")
AI_SUMMARY_MAX_INPUT_ITEMS = int(os.environ.get("AI_SUMMARY_MAX_INPUT_ITEMS", "100"))
AI_SUMMARY_MAX_CHARS = int(os.environ.get("AI_SUMMARY_MAX_CHARS", "1800"))

# State for AI summary scheduling
last_summary_time = None
first_loop_iteration = True  # Force summary on first loop (e.g., after deploy)


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


def get_game_name():
    """Fetch game name from Steam Store API. Returns game name or APPID as fallback."""
    global GAME_NAME_CACHE

    # Return cached value if available
    if GAME_NAME_CACHE:
        return GAME_NAME_CACHE

    try:
        url = f"https://store.steampowered.com/api/appdetails?appids={APPID}"
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get(APPID, {}).get('success'):
            game_name = data[APPID]['data'].get('name', APPID)
            GAME_NAME_CACHE = game_name
            print(f"Fetched game name: {game_name}", flush=True)
            return game_name
        else:
            print(f"WARNING: Could not fetch game name, using APPID", flush=True)
            GAME_NAME_CACHE = APPID
            return APPID
    except Exception as e:
        print(f"ERROR: Failed to fetch game name: {e}, using APPID", flush=True)
        GAME_NAME_CACHE = APPID
        return APPID


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

        # Create metadata table for AI summary state
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        ''')

        conn.commit()
        conn.close()
        print("DB initialized successfully (reviews + discussions + metadata)", flush=True)
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

    # Get game name
    game_name = get_game_name()

    # Build message based on first run or subsequent run
    if stats['is_first_run']:
        msg = (
            f"📌 **{game_name}** (Steam ID: {APPID}) - Reviews Initial Run\n"
            f"📝 Loaded {stats['new_positive'] + stats['new_negative']} initial reviews\n"
            f"👎 {stats['new_negative']} / 👍 {stats['new_positive']}\n"
            f"🕒 UTC: {datetime.utcnow():%Y-%m-%d %H:%M}"
        )
    else:
        msg = (
            f"📌 **{game_name}** (Steam ID: {APPID})\n"
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
        # Find discussion containers
        topic_containers = soup.find_all('div', class_='forum_topic')

        if not topic_containers:
            print("WARNING: No discussion containers found - HTML structure may have changed", flush=True)
            return []

        for topic in topic_containers[:20]:  # Limit to first 20 discussions
            try:
                # Extract discussion link and gid
                link = topic.find('a', class_='forum_topic_overlay')
                if not link:
                    continue

                href = link.get('href', '')

                # Extract gid from URL pattern: .../discussions/0/GIDHERE/
                gid_match = re.search(r'/discussions/\d+/(\d+)/', href)
                if not gid_match:
                    continue

                gid = gid_match.group(1)

                # Extract title from forum_topic_name div
                title_div = topic.find('div', class_='forum_topic_name')
                if not title_div:
                    continue

                # Get title text and remove "PINNED:" label if present
                title = title_div.get_text(strip=True)
                # Remove PINNED: prefix if it exists
                title = re.sub(r'^PINNED:\s*', '', title)

                # Extract author from forum_topic_op div
                author_div = topic.find('div', class_='forum_topic_op')
                author_name = author_div.get_text(strip=True) if author_div else 'Unknown'
                author_steamid = ''  # Not available in list view

                # Timestamp (using current time as fallback per plan)
                timestamp = int(time.time())

                # Reply count from forum_topic_reply_count div
                reply_div = topic.find('div', class_='forum_topic_reply_count')
                reply_count = extract_number(reply_div, '') if reply_div else 0
                view_count = 0  # Not available in list view

                # Check if pinned (has sticky class or sticky_label)
                is_pinned = 1 if (topic.find('span', class_='sticky_label') or 'sticky' in topic.get('class', [])) else 0

                # Content snippet from data-tooltip-forum attribute
                tooltip = topic.get('data-tooltip-forum', '')
                # Extract text from tooltip HTML
                if tooltip:
                    tooltip_soup = BeautifulSoup(tooltip, 'html.parser')
                    topic_hover_text = tooltip_soup.find('div', class_='topic_hover_text')
                    content_snippet = topic_hover_text.get_text(strip=True)[:200] if topic_hover_text else ''
                else:
                    content_snippet = ''

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


# AI Summary Database Functions
def get_metadata(key):
    """Get metadata value by key."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM metadata WHERE key = ?', (key,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else None
    except sqlite3.Error as e:
        print(f"ERROR: Failed to get metadata: {e}", flush=True)
        return None


def set_metadata(key, value):
    """Set metadata value."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        timestamp_now = int(time.time())
        cursor.execute(
            'INSERT OR REPLACE INTO metadata (key, value, updated_at) VALUES (?, ?, ?)',
            (key, value, timestamp_now)
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"ERROR: Failed to set metadata: {e}", flush=True)


def get_reviews_for_summary(since_timestamp=None, days_back=7, limit=100):
    """Query reviews since last summary (or last N days if first run) for AI summary."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        # If we have a timestamp from last summary, use that; otherwise use days_back
        if since_timestamp is not None:
            cutoff_time = since_timestamp
        else:
            cutoff_time = int(time.time()) - (days_back * 24 * 60 * 60)

        cursor.execute('''
            SELECT review, voted_up, timestamp_created
            FROM reviews
            WHERE timestamp_created > ?
            ORDER BY timestamp_created DESC
            LIMIT ?
        ''', (cutoff_time, limit))

        reviews = []
        for row in cursor.fetchall():
            reviews.append({
                'review': row[0],
                'voted_up': row[1],
                'timestamp_created': row[2]
            })

        conn.close()
        return reviews
    except sqlite3.Error as e:
        print(f"ERROR: Failed to fetch reviews for summary: {e}", flush=True)
        return []


def get_discussions_for_summary(since_timestamp=None, days_back=7, limit=100):
    """Query discussions since last summary (or last N days if first run) for AI summary."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        # If we have a timestamp from last summary, use that; otherwise use days_back
        if since_timestamp is not None:
            cutoff_time = since_timestamp
        else:
            cutoff_time = int(time.time()) - (days_back * 24 * 60 * 60)

        cursor.execute('''
            SELECT title, content_snippet, reply_count, timestamp_created
            FROM discussions
            WHERE timestamp_created > ? AND is_pinned = 0
            ORDER BY timestamp_created DESC
            LIMIT ?
        ''', (cutoff_time, limit))

        discussions = []
        for row in cursor.fetchall():
            discussions.append({
                'title': row[0],
                'content_snippet': row[1],
                'reply_count': row[2],
                'timestamp_created': row[3]
            })

        conn.close()
        return discussions
    except sqlite3.Error as e:
        print(f"ERROR: Failed to fetch discussions for summary: {e}", flush=True)
        return []


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

    # Get game name
    game_name = get_game_name()

    # Build message based on first run or subsequent run
    if disc_data['is_first_run']:
        msg = (
            f"📌 **{game_name}** (Steam ID: {APPID}) - Discussions Initial Run\n"
            f"💬 Loaded {disc_data['new_count']} initial discussions\n"
            f"🕒 UTC: {datetime.utcnow():%Y-%m-%d %H:%M}"
        )
    else:
        # Build detailed list with title and snippet
        msg = f"📌 **{game_name}** (Steam ID: {APPID}) - New Discussions\n"
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


# AI Summary Functions
def format_reviews_for_prompt(reviews):
    """Format reviews for inclusion in OpenAI prompt."""
    formatted = []
    for r in reviews:
        text = r.get('review', '')[:200] if r.get('review') else "No text"
        formatted.append(f"- {text}")
    return "\n".join(formatted)


def format_discussions_for_prompt(discussions):
    """Format discussions for inclusion in OpenAI prompt."""
    formatted = []
    for d in discussions:
        title = d.get('title', '')[:100]
        snippet = d.get('content_snippet', '')[:150] if d.get('content_snippet') else ""
        replies = d.get('reply_count', 0)
        formatted.append(f"- {title} ({replies} replies)\n  {snippet}")
    return "\n".join(formatted)


def build_summary_prompt(reviews_data, discussions_data, is_first_run=False):
    """Build structured prompt for OpenAI API."""
    game_name = get_game_name()

    # Count positive/negative
    positive = [r for r in reviews_data if r.get('voted_up')]
    negative = [r for r in reviews_data if not r.get('voted_up')]

    # Adjust time context based on whether this is first run or incremental
    if is_first_run:
        time_context = f"from the last {AI_SUMMARY_DAYS_LOOKBACK} days"
    else:
        time_context = "since the last check"

    prompt = f"""Analyze the following NEW Steam community feedback for {game_name} {time_context}.

REVIEWS ({len(reviews_data)} total: {len(positive)} positive, {len(negative)} negative):

POSITIVE REVIEWS:
{format_reviews_for_prompt(positive[:30])}

NEGATIVE REVIEWS:
{format_reviews_for_prompt(negative[:30])}

DISCUSSIONS ({len(discussions_data)} topics):
{format_discussions_for_prompt(discussions_data[:40])}

Please provide a comprehensive summary covering:
1. Best feedback and highlights (what players love)
2. Worst feedback and pain points (what players dislike)
3. Recurring problems or themes (mentioned multiple times)
4. Overall sentiment analysis (trend: improving/declining/stable)

Keep the summary under 1000 words and focus on actionable insights for developers."""

    return prompt


def generate_ai_summary(reviews_data, discussions_data, is_first_run=False):
    """Generate AI summary using OpenAI API."""
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)

        # Build prompt with structured data
        prompt = build_summary_prompt(reviews_data, discussions_data, is_first_run)

        # API call with configured model
        max_tokens = int(AI_SUMMARY_MAX_CHARS / 0.75)  # ~4 chars per token

        response = client.chat.completions.create(
            model=AI_SUMMARY_MODEL,
            messages=[
                {"role": "system", "content": "You are a game community analyst who summarizes player feedback to help developers understand community sentiment and key issues."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=max_tokens,
            temperature=0.7
        )

        summary = response.choices[0].message.content

        # Log token usage for cost tracking
        usage = response.usage
        cost_input = usage.prompt_tokens * 0.00000015 if AI_SUMMARY_MODEL == "gpt-4o-mini" else usage.prompt_tokens * 0.0000025
        cost_output = usage.completion_tokens * 0.0000006 if AI_SUMMARY_MODEL == "gpt-4o-mini" else usage.completion_tokens * 0.00001
        total_cost = cost_input + cost_output
        print(f"OpenAI tokens: input={usage.prompt_tokens}, output={usage.completion_tokens}, cost=${total_cost:.4f}", flush=True)

        # Trim to max length if needed
        if len(summary) > AI_SUMMARY_MAX_CHARS:
            summary = summary[:AI_SUMMARY_MAX_CHARS-3] + "..."

        return summary

    except Exception as e:
        print(f"ERROR: OpenAI API failed: {e}", flush=True)
        return None


def should_generate_summary():
    """Check if it's time to generate an AI summary."""
    global last_summary_time, first_loop_iteration

    # Feature disabled
    if not ENABLE_AI_SUMMARY:
        return False

    # No API key configured
    if not OPENAI_API_KEY:
        print("WARNING: AI summary enabled but no OPENAI_API_KEY set", flush=True)
        return False

    # Force summary on first loop iteration (e.g., after deploy/restart)
    if first_loop_iteration:
        print("First loop iteration - forcing AI summary generation", flush=True)
        return True

    # Load last summary time from DB (if not already loaded)
    if last_summary_time is None:
        last_summary_str = get_metadata("last_summary_timestamp")
        if last_summary_str:
            last_summary_time = int(last_summary_str)

    # Calculate interval
    interval_seconds = AI_SUMMARY_INTERVAL_HOURS * 3600
    current_time = int(time.time())

    # First run or interval elapsed
    if last_summary_time is None:
        return True

    return (current_time - last_summary_time) >= interval_seconds


def fetch_and_generate_summary():
    """Fetch data and generate AI summary."""
    global last_summary_time

    try:
        # Get timestamp of last summary
        last_summary_str = get_metadata("last_summary_timestamp")
        since_timestamp = int(last_summary_str) if last_summary_str else None

        # Fetch data for summary (since last summary, or last N days if first run)
        reviews_data = get_reviews_for_summary(
            since_timestamp=since_timestamp,
            days_back=AI_SUMMARY_DAYS_LOOKBACK,
            limit=AI_SUMMARY_MAX_INPUT_ITEMS
        )
        discussions_data = get_discussions_for_summary(
            since_timestamp=since_timestamp,
            days_back=AI_SUMMARY_DAYS_LOOKBACK,
            limit=AI_SUMMARY_MAX_INPUT_ITEMS
        )

        # Check if we have any data
        if not reviews_data and not discussions_data:
            if since_timestamp:
                print(f"SKIP No new data for AI summary since last check", flush=True)
            else:
                print(f"SKIP No data for AI summary (last {AI_SUMMARY_DAYS_LOOKBACK} days)", flush=True)
            return None

        # Log what we're analyzing
        if since_timestamp:
            time_range = f"since last check ({int((time.time() - since_timestamp) / 3600)}h ago)"
        else:
            time_range = f"last {AI_SUMMARY_DAYS_LOOKBACK} days (first run)"

        print(f"Generating AI summary: {len(reviews_data)} reviews, {len(discussions_data)} discussions ({time_range})", flush=True)

        # Generate summary
        summary = generate_ai_summary(reviews_data, discussions_data, since_timestamp is None)

        if summary:
            # Update last summary time
            last_summary_time = int(time.time())
            set_metadata("last_summary_timestamp", str(last_summary_time))

            return {
                'summary': summary,
                'review_count': len(reviews_data),
                'discussion_count': len(discussions_data),
                'time_range': time_range
            }

        return None

    except Exception as e:
        print(f"ERROR: AI summary generation failed: {e}", flush=True)
        return None


def split_text_smart(text, max_length):
    """Split text at paragraph boundaries for readability."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    current = ""

    # Split by paragraphs first
    paragraphs = text.split("\n\n")

    for para in paragraphs:
        if len(current) + len(para) + 2 <= max_length:
            current += para + "\n\n"
        else:
            if current:
                chunks.append(current.strip())
            current = para + "\n\n"

    if current:
        chunks.append(current.strip())

    return chunks


def send_discord_message(msg):
    """Send single message to Discord webhook."""
    try:
        response = requests.post(WEBHOOK, json={"content": msg}, timeout=30)
        response.raise_for_status()
        print(f"OK Discord message sent ({len(msg)} chars)", flush=True)
    except requests.RequestException as e:
        print(f"ERROR: Discord webhook failed: {e}", flush=True)


def post_ai_summary_notification(summary_data):
    """Send AI summary to Discord (may split into multiple messages)."""
    if not summary_data:
        return

    game_name = get_game_name()
    summary_text = summary_data['summary']

    # Build header
    header = (
        f"🤖 **AI Summary - {game_name}** (Steam ID: {APPID})\n"
        f"📊 Analyzed: {summary_data['review_count']} reviews, "
        f"{summary_data['discussion_count']} discussions "
        f"({summary_data['time_range']})\n"
        f"🕒 UTC: {datetime.utcnow():%Y-%m-%d %H:%M}\n\n"
    )

    # Calculate available space for summary (leave room for header)
    max_msg_len = 1950  # Safe margin under 2000
    header_len = len(header)

    # Split summary if needed
    if header_len + len(summary_text) <= max_msg_len:
        # Single message
        send_discord_message(header + summary_text)
    else:
        # Multiple messages
        send_discord_message(header + "(Summary split into multiple parts)")

        # Split summary into chunks
        chunk_size = max_msg_len - 50  # Room for "Part X/Y"
        chunks = split_text_smart(summary_text, chunk_size)

        for i, chunk in enumerate(chunks, 1):
            prefix = f"**[Part {i}/{len(chunks)}]**\n\n" if len(chunks) > 1 else ""
            send_discord_message(prefix + chunk)
            time.sleep(1)  # Rate limit courtesy


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

        # Check if time for AI summary
        if should_generate_summary():
            summary_data = fetch_and_generate_summary()
            if summary_data:
                post_ai_summary_notification(summary_data)

        # Mark first iteration as complete
        first_loop_iteration = False

    except Exception as e:
        print(f"ERROR Unexpected error: {repr(e)}", flush=True)

    time.sleep(CHECK_MIN * 60)
