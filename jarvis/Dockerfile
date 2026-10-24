FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

RUN groupadd --system --gid 10001 abuai \
    && useradd --system --uid 10001 --gid abuai --no-create-home --shell /usr/sbin/nologin abuai \
    && mkdir -p /data \
    && chown abuai:abuai /data

COPY --chown=root:root app.py qr.py ./
COPY --chown=root:root templates ./templates
COPY --chown=root:root static ./static

USER abuai
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/login', timeout=4).status == 200 else 1)" || exit 1

CMD ["gunicorn", "--workers", "2", "--timeout", "120", "--bind", "0.0.0.0:8080", \
     "--access-logfile", "-", "--error-logfile", "-", "app:create_app()"]
