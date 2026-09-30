FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

RUN apt-get update && apt-get install -y xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99

CMD ["bash", "-c", "Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp & sleep 2 && python -u renwe.py"]
