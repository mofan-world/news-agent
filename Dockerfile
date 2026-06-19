FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY src ./src
COPY WW_verify_u6b1njoAirZrz9ot.txt ./

VOLUME ["/data"]
EXPOSE 8080

CMD ["python", "-m", "news_agent.main"]
