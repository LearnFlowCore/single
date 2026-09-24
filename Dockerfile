FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOCALAPPDATA=/data \
    AUTOPOSTER_WEB_HOST=0.0.0.0 \
    AUTOPOSTER_WEB_PORT=8000

WORKDIR /app
COPY requirements-web.txt .
RUN pip install --no-cache-dir -r requirements-web.txt
COPY . .
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000 8080
CMD ["python", "web_main.py"]
