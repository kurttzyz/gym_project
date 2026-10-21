FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /var/run/celery && chown app:app /var/run/celery
COPY --chown=app:app . .
USER app
CMD ["celery", "-A", "platform_config.celery:app", "worker", "--loglevel=info", "--concurrency=2"]
