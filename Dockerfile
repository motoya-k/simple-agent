# simple-agent — the agent only. MCP servers you add (Google Workspace, ...)
# belong in an image built FROM this one; see README "Deploy".
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY simple_agent ./simple_agent
RUN pip install ".[postgres]" && useradd --create-home --uid 10001 agent

USER agent
WORKDIR /home/agent
# Created by the agent user, so files a derived image COPYs in — an mcp.json,
# a profile — do not leave the directory owned by root.
RUN mkdir -p /home/agent/.simple-agent/profiles

# Production defaults; override per deployment.
#   `pip install --user` in a derived image lands on PATH, no shell on an
#   unattended host, one JSON object per log line,
#   the AWS region the Bedrock provider and AWS named profiles default to.
ENV PATH=/home/agent/.local/bin:$PATH \
    SIMPLE_AGENT_HOME=/home/agent/.simple-agent \
    SIMPLE_AGENT_LOG_FORMAT=json \
    SIMPLE_AGENT_DISABLED_TOOLS=terminal \
    AWS_REGION=ap-northeast-1

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["simple-agent", "--health"]

CMD ["simple-agent", "--email"]
