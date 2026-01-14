#!/bin/bash
# Trigger an immediate sync (local or remote)

set -e

# Configuration
REMOTE_HOST="${REMOTE_HOST:-raspberrypi}"
REMOTE_USER="${REMOTE_USER:-pi}"
REMOTE_PATH="${REMOTE_PATH:-~/CalendarSync}"

# Use docker compose (v2) or docker-compose (v1) depending on availability
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

# Default to local
MODE="${1:-local}"

case "$MODE" in
    local)
        echo "→ Triggering local sync now..."
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        $DOCKER_COMPOSE exec calendar-sync python sync_once.py
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "✓ Sync complete"
        ;;

    remote)
        echo "→ Triggering remote sync on $REMOTE_USER@$REMOTE_HOST..."
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ssh -t "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker compose exec -T calendar-sync python sync_once.py"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "✓ Sync complete"
        ;;

    *)
        echo "Usage: $0 [local|remote]"
        echo ""
        echo "Examples:"
        echo "  $0              # Trigger local sync (default)"
        echo "  $0 local        # Trigger local sync"
        echo "  $0 remote       # Trigger remote sync"
        echo ""
        echo "Environment variables:"
        echo "  REMOTE_HOST     # Default: raspberrypi"
        echo "  REMOTE_USER     # Default: pi"
        echo "  REMOTE_PATH     # Default: ~/CalendarSync"
        exit 1
        ;;
esac
