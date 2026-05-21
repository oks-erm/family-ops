FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN groupadd --system appgroup \
    && useradd --system --gid appgroup --no-create-home appuser \
    && chmod +x scripts/entrypoint.sh \
    && chown -R appuser:appgroup /app

USER appuser

ENTRYPOINT ["scripts/entrypoint.sh"]
