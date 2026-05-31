FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY server.py ./

RUN useradd --create-home --shell /bin/bash mcp \
    && chown -R mcp:mcp /app
USER mcp

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/health')" || exit 1

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "3000"]
