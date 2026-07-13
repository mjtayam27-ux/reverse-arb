# Multi-stage Dockerfile for Polymarket Reverse Arbitrage Bot
# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libsecp256k1-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY pyproject.toml ./
RUN pip install --no-cache-dir --prefix=/install -e .

# Runtime stage
FROM python:3.11-slim AS runtime

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsecp256k1-0 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY src/ ./src/
COPY config/ ./config/
COPY deploy/ ./deploy/

# Create non-root user
RUN useradd --no-create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# Environment defaults (overridden by Fly secrets)
ENV POLYMARKET_EXCHANGE_ADDRESS=0x4bFb45d408abC010C7d6E7f50e2AE8f3D8E8E9B3
ENV POLYMARKET_COLLATERAL_TOKEN=0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

# API and Metrics ports
EXPOSE 8080 9090

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8080/health', timeout=5).raise_for_status()"

# Entry point
ENTRYPOINT ["python", "-m", "deploy.entry"]