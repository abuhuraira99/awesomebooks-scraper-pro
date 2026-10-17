FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ABP_HOST=0.0.0.0 \
    ABP_PORT=7642 \
    ABP_DATA_DIR=/app/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY awesomebooks_pro ./awesomebooks_pro
RUN useradd --create-home --uid 1000 scraper && mkdir -p /app/data && chown -R scraper /app
USER scraper

VOLUME ["/app/data"]
EXPOSE 7642
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7642/api/health', timeout=4).status == 200 else 1)"
CMD ["python", "-m", "awesomebooks_pro"]
