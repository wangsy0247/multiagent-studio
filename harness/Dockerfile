FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy harness code
COPY . .

# Create workspace and memory directories
RUN mkdir -p /app/workspace /app/memory

EXPOSE 8001

ENV HARNESS_PORT=8001
ENV HARNESS_WORKSPACE_ROOT=/app/workspace
ENV HARNESS_MEMORY_ROOT=/app/memory

CMD ["python", "-m", "harness.main"]
