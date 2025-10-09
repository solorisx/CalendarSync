#!/bin/bash

echo "=== Calendar Sync Docker Setup ==="
echo ""

# Create data directory
mkdir -p data

# Copy config template
if [ ! -f data/config.json ]; then
    cp data/config.json.template data/config.json
    echo "✓ Created data/config.json - please edit with your credentials"
fi

echo ""
echo "Setup complete! Next steps:"
echo ""
echo "1. Get Google Calendar credentials:"
echo "   - Go to: https://console.cloud.google.com/"
echo "   - Create OAuth 2.0 credentials (Desktop app)"
echo "   - Download as 'credentials.json' to ./data/"
echo ""
echo "2. Get iCloud app-specific password:"
echo "   - Go to: https://appleid.apple.com/"
echo "   - Generate app-specific password under Security"
echo ""
echo "3. Edit configuration:"
echo "   nano data/config.json"
echo ""
echo "4. For FIRST RUN ONLY (Google OAuth):"
echo "   - Uncomment ports section in docker-compose.yml"
echo "   - Run: docker-compose up"
echo "   - Follow OAuth flow in browser"
echo "   - After successful auth, stop container (Ctrl+C)"
echo "   - Comment out ports section again"
echo ""
echo "5. Start the service:"
echo "   docker-compose up -d"
echo ""
echo "6. View logs:"
echo "   docker-compose logs -f"
echo ""
echo "7. Stop the service:"
echo "   docker-compose down"
echo ""
