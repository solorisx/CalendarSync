#!/bin/bash
# View logs from CalendarSync service (local or remote)

set -e

# Configuration
REMOTE_HOST="${REMOTE_HOST:-raspberrypi}"
REMOTE_USER="${REMOTE_USER:-pi}"
REMOTE_PATH="${REMOTE_PATH:-~/CalendarSync}"

# Default to local
MODE="${1:-local}"
TAIL_LINES="${2:-50}"

case "$MODE" in
    local)
        echo "→ Viewing local logs (last $TAIL_LINES lines, following)..."
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        docker-compose logs -f --tail="$TAIL_LINES"
        ;;

    remote)
        echo "→ Viewing remote logs from $REMOTE_USER@$REMOTE_HOST (last $TAIL_LINES lines, following)..."
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        ssh -t "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker-compose logs -f --tail=$TAIL_LINES"
        ;;

    *)
        echo "Usage: $0 [local|remote] [tail_lines]"
        echo ""
        echo "Examples:"
        echo "  $0              # View local logs (default)"
        echo "  $0 local        # View local logs"
        echo "  $0 local 100    # View last 100 lines from local"
        echo "  $0 remote       # View remote logs"
        echo "  $0 remote 100   # View last 100 lines from remote"
        echo ""
        echo "Environment variables:"
        echo "  REMOTE_HOST     # Default: raspberrypi"
        echo "  REMOTE_USER     # Default: pi"
        echo "  REMOTE_PATH     # Default: ~/CalendarSync"
        exit 1
        ;;
esac
