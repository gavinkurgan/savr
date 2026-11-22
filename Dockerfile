FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    DATABASE_PATH=/data/savr.db

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The recipe library (app/data/library.json) ships inside the image.
COPY app ./app
COPY scripts ./scripts
COPY wsgi.py recipe_sites.txt ./

RUN useradd --create-home appuser && mkdir -p /data && chown -R appuser /data /srv/app/data
USER appuser
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"

CMD gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 60 wsgi:app
