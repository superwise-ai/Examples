FROM python:3.11-slim

WORKDIR /app

# Install package and dependencies
COPY pyproject.toml sw_sentinel.py sentinel_config.json.example ./
RUN pip install --no-cache-dir .

# Configuration — two options:
#
#   Option 1: Mount a config file
#     docker run -v /path/to/sentinel_config.json:/app/sentinel_config.json ...
#
#   Option 2: Set environment variables
#     docker run \
#       -e SUPERWISE_CLIENT_ID=your_id \
#       -e SUPERWISE_CLIENT_SECRET=your_secret \
#       -e ANTHROPIC_API_KEY=sk-ant-... \
#       ...

ENV SENTINEL_HOST=0.0.0.0
ENV SENTINEL_PORT=8080

EXPOSE 8080

CMD ["sw-sentinel", "--config", "/app/sentinel_config.json"]
