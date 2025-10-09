FROM python:3.11-slim

WORKDIR /app

# Install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY sync_calendars.py .
COPY sync_once.py .

# Create volume mount points
VOLUME ["/app/data"]

# Run sync script on a schedule
CMD ["python", "-u", "sync_calendars.py"]
