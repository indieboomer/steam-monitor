#!/usr/bin/env bash
set -euo pipefail

# Database inspection helper script for steam-monitor
# Usage: ./db_inspect.sh [command]
# If no command provided, shows interactive menu

CONTAINER="steam-monitor"
DB_PATH="/data/reviews.db"

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

function run_query() {
    local query="$1"
    docker exec "$CONTAINER" python -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('''$query''')
for row in cursor.fetchall():
    print(row)
conn.close()
"
}

function print_section() {
    echo -e "\n${BLUE}=== $1 ===${NC}"
}

function show_stats() {
    print_section "Database Statistics"

    echo -e "${GREEN}Reviews:${NC}"
    docker exec "$CONTAINER" python -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM reviews')
print(f'  Total: {cursor.fetchone()[0]}')
cursor.execute('SELECT voted_up, COUNT(*) FROM reviews GROUP BY voted_up')
for row in cursor.fetchall():
    label = 'Positive' if row[0] else 'Negative'
    print(f'  {label}: {row[1]}')
conn.close()
"

    echo -e "${GREEN}Discussions:${NC}"
    docker exec "$CONTAINER" python -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT COUNT(*) FROM discussions')
print(f'  Total: {cursor.fetchone()[0]}')
cursor.execute('SELECT COUNT(*) FROM discussions WHERE is_pinned = 1')
print(f'  Pinned: {cursor.fetchone()[0]}')
conn.close()
"
}

function show_recent_reviews() {
    print_section "Recent Reviews (Last 5)"
    docker exec "$CONTAINER" python -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('''
    SELECT voted_up, timestamp_fetched, author_steamid
    FROM reviews
    ORDER BY timestamp_fetched DESC
    LIMIT 5
''')
for i, row in enumerate(cursor.fetchall(), 1):
    vote = '👍 Positive' if row[0] else '👎 Negative'
    timestamp = datetime.fromtimestamp(row[1]).strftime('%Y-%m-%d %H:%M')
    print(f'{i}. {vote} | {timestamp} | {row[2][:16]}...')
conn.close()
"
}

function show_recent_discussions() {
    print_section "Recent Discussions (Last 5)"
    docker exec "$CONTAINER" python -c "
import sqlite3
from datetime import datetime
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('''
    SELECT title, author_name, timestamp_fetched, is_pinned
    FROM discussions
    ORDER BY timestamp_fetched DESC
    LIMIT 5
''')
for i, row in enumerate(cursor.fetchall(), 1):
    pinned = ' 📌' if row[3] else ''
    timestamp = datetime.fromtimestamp(row[2]).strftime('%Y-%m-%d %H:%M')
    title = row[0][:60] + '...' if len(row[0]) > 60 else row[0]
    print(f'{i}. {title}{pinned}')
    print(f'   By: {row[1]} | {timestamp}')
conn.close()
"
}

function show_schema() {
    print_section "Database Schema"

    echo -e "${GREEN}Reviews Table:${NC}"
    docker exec "$CONTAINER" python -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT sql FROM sqlite_master WHERE type=\"table\" AND name=\"reviews\"')
result = cursor.fetchone()
if result:
    print(result[0])
conn.close()
"

    echo ""
    echo -e "${GREEN}Discussions Table:${NC}"
    docker exec "$CONTAINER" python -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT sql FROM sqlite_master WHERE type=\"table\" AND name=\"discussions\"')
result = cursor.fetchone()
if result:
    print(result[0])
conn.close()
"
}

function show_db_info() {
    print_section "Database File Info"
    docker exec "$CONTAINER" ls -lh "$DB_PATH" 2>/dev/null || echo "Database file not found"

    docker exec "$CONTAINER" python -c "
import sqlite3, os
if os.path.exists('$DB_PATH'):
    size = os.path.getsize('$DB_PATH')
    print(f'Size: {size:,} bytes ({size/1024:.2f} KB)')
    conn = sqlite3.connect('$DB_PATH')
    cursor = conn.cursor()
    cursor.execute('PRAGMA page_count')
    pages = cursor.fetchone()[0]
    cursor.execute('PRAGMA page_size')
    page_size = cursor.fetchone()[0]
    print(f'Pages: {pages:,} (page size: {page_size} bytes)')
    conn.close()
"
}

function search_discussions() {
    local keyword="$1"
    print_section "Searching Discussions for: $keyword"
    docker exec "$CONTAINER" python -c "
import sqlite3
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('''
    SELECT title, author_name, content_snippet
    FROM discussions
    WHERE title LIKE ? OR content_snippet LIKE ?
    ORDER BY timestamp_fetched DESC
    LIMIT 10
''', ('%$keyword%', '%$keyword%'))
for i, row in enumerate(cursor.fetchall(), 1):
    print(f'{i}. {row[0]}')
    print(f'   By: {row[1]}')
    if row[2]:
        snippet = row[2][:100] + '...' if len(row[2]) > 100 else row[2]
        print(f'   {snippet}')
    print()
conn.close()
"
}

function export_csv() {
    local table="$1"
    local filename="${table}_export_$(date +%Y%m%d_%H%M%S).csv"

    print_section "Exporting $table to $filename"
    docker exec "$CONTAINER" python -c "
import sqlite3
import csv
import sys
conn = sqlite3.connect('$DB_PATH')
cursor = conn.cursor()
cursor.execute('SELECT * FROM $table')
rows = cursor.fetchall()
if rows:
    # Get column names
    cursor.execute('PRAGMA table_info($table)')
    columns = [col[1] for col in cursor.fetchall()]

    # Write CSV to stdout
    writer = csv.writer(sys.stdout)
    writer.writerow(columns)
    writer.writerows(rows)
conn.close()
" > "$filename"
    echo "Exported ${#rows[@]} rows to $filename"
}

function show_menu() {
    echo -e "${YELLOW}Steam Monitor - Database Inspector${NC}"
    echo ""
    echo "Available commands:"
    echo "  stats       - Show database statistics"
    echo "  reviews     - Show recent reviews"
    echo "  discussions - Show recent discussions"
    echo "  schema      - Show database schema"
    echo "  info        - Show database file info"
    echo "  search      - Search discussions (usage: ./db_inspect.sh search <keyword>)"
    echo "  export      - Export table to CSV (usage: ./db_inspect.sh export <reviews|discussions>)"
    echo "  all         - Show all information"
    echo ""
    echo "Examples:"
    echo "  ./db_inspect.sh stats"
    echo "  ./db_inspect.sh search \"crash\""
    echo "  ./db_inspect.sh export reviews"
}

# Main script logic
case "${1:-menu}" in
    stats)
        show_stats
        ;;
    reviews)
        show_recent_reviews
        ;;
    discussions)
        show_recent_discussions
        ;;
    schema)
        show_schema
        ;;
    info)
        show_db_info
        ;;
    search)
        if [ -z "${2:-}" ]; then
            echo "Error: Please provide a search keyword"
            echo "Usage: ./db_inspect.sh search <keyword>"
            exit 1
        fi
        search_discussions "$2"
        ;;
    export)
        if [ -z "${2:-}" ]; then
            echo "Error: Please provide a table name (reviews or discussions)"
            echo "Usage: ./db_inspect.sh export <reviews|discussions>"
            exit 1
        fi
        export_csv "$2"
        ;;
    all)
        show_db_info
        show_stats
        show_recent_reviews
        show_recent_discussions
        ;;
    menu|help|--help|-h)
        show_menu
        ;;
    *)
        echo "Error: Unknown command '$1'"
        echo ""
        show_menu
        exit 1
        ;;
esac
