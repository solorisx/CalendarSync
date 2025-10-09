#!/bin/bash
set -e

# Configuration
REMOTE_HOST="${REMOTE_HOST:-raspberrypi}"
REMOTE_USER="${REMOTE_USER:-pi}"
REMOTE_PATH="${REMOTE_PATH:-~/CalendarSync}"

echo "======================================"
echo "CalendarSync - Raspberry Pi Deployment"
echo "======================================"
echo "Remote: $REMOTE_USER@$REMOTE_HOST"
echo "Path: $REMOTE_PATH"
echo ""

# Check if we can reach the remote host
echo "→ Testing connection to $REMOTE_HOST..."
if ! ssh -o ConnectTimeout=5 "$REMOTE_USER@$REMOTE_HOST" "echo 'Connection successful'" 2>/dev/null; then
    echo "✗ Error: Cannot connect to $REMOTE_USER@$REMOTE_HOST"
    echo "  Make sure SSH is configured and the host is reachable"
    exit 1
fi
echo "✓ Connection successful"
echo ""

# Create remote directory if it doesn't exist
echo "→ Setting up remote directory..."
ssh "$REMOTE_USER@$REMOTE_HOST" "mkdir -p $REMOTE_PATH"
echo "✓ Remote directory ready"
echo ""

# Sync files to Raspberry Pi (excluding data directory and git files)
echo "→ Syncing files to Raspberry Pi..."
rsync -avz --delete \
    --exclude '.git' \
    --exclude '.gitignore' \
    --exclude 'data/' \
    --exclude 'venv/' \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    --exclude '.env' \
    --exclude 'CLAUDE.md' \
    ./ "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/"
echo "✓ Files synced"
echo ""

# Check if data directory exists on remote
echo "→ Checking remote data directory..."
if ssh "$REMOTE_USER@$REMOTE_HOST" "[ ! -d $REMOTE_PATH/data ]"; then
    echo "⚠ Warning: data/ directory not found on remote"
    echo "  Creating directory and config template..."
    ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && bash setup.sh"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "⚠ IMPORTANT: First-time setup required!"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "1. Copy your credentials to the Pi:"
    echo "   scp data/credentials.json $REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/data/"
    echo "   scp data/token.pickle $REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/data/"
    echo ""
    echo "2. Edit the config on the Pi:"
    echo "   ssh $REMOTE_USER@$REMOTE_HOST"
    echo "   cd $REMOTE_PATH"
    echo "   nano data/config.json"
    echo ""
    echo "3. Run this deploy script again"
    echo ""
    exit 0
else
    echo "✓ Data directory exists"
fi
echo ""

# Stop existing container if running
echo "→ Stopping existing containers..."
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker-compose down 2>/dev/null || true"
echo "✓ Containers stopped"
echo ""

# Build and start containers
echo "→ Building and starting containers..."
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker-compose build && docker-compose up -d"
echo "✓ Containers started"
echo ""

# Show status
echo "→ Container status:"
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker-compose ps"
echo ""

# Show recent logs
echo "→ Recent logs (last 20 lines):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker-compose logs --tail=20"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "✓ Deployment complete!"
echo ""
echo "Useful commands:"
echo "  View logs: ssh $REMOTE_USER@$REMOTE_HOST 'cd $REMOTE_PATH && docker-compose logs -f'"
echo "  Manual sync: ssh $REMOTE_USER@$REMOTE_HOST 'cd $REMOTE_PATH && ./sync_now.sh'"
echo "  Stop service: ssh $REMOTE_USER@$REMOTE_HOST 'cd $REMOTE_PATH && docker-compose down'"
echo ""
