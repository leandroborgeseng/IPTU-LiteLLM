FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl poppler-utils tesseract-ocr tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py batch.py parecer.py lotes.html lotes.js ./
RUN install -d -m 0700 -o 10001 -g 10001 /data
COPY fixtures/ ./fixtures/
USER 10001:10001
EXPOSE 8080
CMD ["python", "app.py"]
