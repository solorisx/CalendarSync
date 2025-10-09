#!/bin/bash
# Run an immediate one-time sync using the Docker container

echo "Running immediate calendar sync..."
docker-compose run --rm calendar-sync python sync_once.py
