#!/bin/bash
set -e

# Load .env file if it exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

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

# Check if Docker is installed on the Pi
echo "→ Checking Docker installation..."
if ! ssh "$REMOTE_USER@$REMOTE_HOST" "command -v docker &> /dev/null"; then
    echo "⚠ Docker not found on remote host"
    echo "→ Installing Docker..."
    ssh "$REMOTE_USER@$REMOTE_HOST" "curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh && sudo usermod -aG docker $REMOTE_USER && rm get-docker.sh"
    echo "✓ Docker installed"
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "⚠ IMPORTANT: Docker group membership updated!"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "You need to log out and back in to the Pi for group changes to take effect."
    echo "Run this command to apply changes without logging out:"
    echo "  ssh $REMOTE_USER@$REMOTE_HOST 'newgrp docker'"
    echo ""
    echo "Then run this deploy script again."
    echo ""
    exit 0
else
    echo "✓ Docker is installed"
fi
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
    ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && bash ./scripts/setup.sh"
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
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker compose down 2>/dev/null || true"
echo "✓ Containers stopped"
echo ""

# Build and start containers
echo "→ Building and starting containers..."
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker compose build && docker compose up -d"
echo "✓ Containers started"
echo ""

# Show status
echo "→ Container status:"
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker compose ps"
echo ""

# Show recent logs
echo "→ Recent logs (last 20 lines):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && docker compose logs --tail=20"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "✓ Deployment complete!"
echo ""
echo "Useful commands:"
echo "  View logs: ssh $REMOTE_USER@$REMOTE_HOST 'cd $REMOTE_PATH && docker compose logs -f'"
echo "  Manual sync: ssh $REMOTE_USER@$REMOTE_HOST 'cd $REMOTE_PATH && ./sync_now.sh'"
echo "  Stop service: ssh $REMOTE_USER@$REMOTE_HOST 'cd $REMOTE_PATH && docker compose down'"
echo ""
