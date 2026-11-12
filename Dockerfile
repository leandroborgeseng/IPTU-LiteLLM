FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py .
COPY fixtures/ ./fixtures/
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
