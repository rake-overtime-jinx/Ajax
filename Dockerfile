FROM python:3.10-slim

WORKDIR /app

ENV XRAY_LOCATION_ASSET=/usr/local/bin/xray
ENV PATH="/usr/local/bin/xray:${PATH}"
ENV PYTHONUNBUFFERED=1


RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx wget unzip procps curl ca-certificates && \
    rm -rf /etc/nginx/sites-enabled/* /etc/nginx/conf.d/* && \
    mkdir -p /usr/local/bin/xray /run /var/log/nginx /tmp && \
    wget -q https://github.com/XTLS/Xray-core/releases/download/v1.8.24/Xray-linux-64.zip -O /tmp/xray.zip && \
    unzip -q /tmp/xray.zip -d /usr/local/bin/xray && \
    chmod +x /usr/local/bin/xray/xray && \
    rm -f /tmp/xray.zip && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .


RUN mkdir -p /var/log/nginx /run && \
    chmod -R 777 /var/log/nginx /run /app

ENV PORT=8080
EXPOSE 8080

CMD ["python", "app.py"]
