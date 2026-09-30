FROM mcr.microsoft.com/playwright/python:v1.52.0-noble

RUN apt-get update && apt-get install -y xvfb

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

ENV PYTHONUNBUFFERED=1
# NOT setting RENWE_HEADLESS — leave it unset so cloud mode does not force it

CMD ["xvfb-run", "-a", "-s", "-screen 0 1920x1080x24", "python", "-u", "renwe.py"]
