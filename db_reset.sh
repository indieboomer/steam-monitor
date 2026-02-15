#!/usr/bin/env bash
set -euo pipefail

# Database reset script for steam-monitor
# This script will delete all collected data and start fresh

CONTAINER="steam-monitor"
DB_FILE="./data/reviews.db"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}Steam Monitor - Database Reset${NC}"
echo -e "${YELLOW}========================================${NC}"
echo ""
echo -e "${RED}WARNING: This will delete ALL collected data!${NC}"
echo -e "${RED}This includes:${NC}"
echo -e "${RED}  - All review records${NC}"
echo -e "${RED}  - All discussion records${NC}"
echo ""
echo -e "Database file: ${DB_FILE}"
echo ""
read -p "Are you sure you want to proceed? (type 'yes' to confirm): " -r
echo ""

if [[ ! $REPLY =~ ^[Yy][Ee][Ss]$ ]]; then
    echo -e "${GREEN}Aborted. No changes made.${NC}"
    exit 0
fi

echo -e "${YELLOW}Step 1: Stopping container...${NC}"
if docker ps -q -f name="^${CONTAINER}$" | grep -q .; then
    docker stop "$CONTAINER"
    echo -e "${GREEN}✓ Container stopped${NC}"
else
    echo -e "${YELLOW}! Container not running${NC}"
fi

echo ""
echo -e "${YELLOW}Step 2: Removing database file...${NC}"
if [ -f "$DB_FILE" ]; then
    rm "$DB_FILE"
    echo -e "${GREEN}✓ Database file deleted${NC}"
else
    echo -e "${YELLOW}! Database file not found (already clean)${NC}"
fi

echo ""
echo -e "${YELLOW}Step 3: Starting container...${NC}"
docker start "$CONTAINER"
echo -e "${GREEN}✓ Container started${NC}"

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Database reset complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Next check cycle will:"
echo "  - Create fresh database"
echo "  - Load initial reviews (20)"
echo "  - Load initial discussions (up to 20)"
echo "  - Send 'Initial Run' notifications to Discord"
echo ""
echo "Monitor logs with: docker logs -f $CONTAINER"
