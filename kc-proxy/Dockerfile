# ============================================================
# Dockerfile — Keycloak Admin Proxy
# Multi-stage: slim Python image, distroless runtime
# ============================================================

FROM python:3.11-slim AS base

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Run as non-root
RUN useradd -r -u 10001 -m proxyuser && chown -R proxyuser:proxyuser /app
USER proxyuser

EXPOSE 8200

# Single worker — request forward là I/O bound, không cần multi-process
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200", "--workers", "2"]