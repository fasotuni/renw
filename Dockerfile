FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

WORKDIR /app

# system deps that the playwright base already has: skip apt-get entirely.
# just install our python deps.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The playwright base image already sets PLAYWRIGHT_BROWSERS_PATH and has
# chromium + ffmpeg pre-baked, so no `playwright install` step is needed.
# That saves ~30s per cold build and ~400 MB in the layer cache.

# Stream stdout so Railway's log pane shows our prints live.
ENV PYTHONUNBUFFERED=1
ENV RENWE_HEADLESS=1

CMD ["python", "-u", "renwe.py"]
