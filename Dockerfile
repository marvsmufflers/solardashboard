FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt pyproject.toml README.md ./
RUN pip install -r requirements.txt
COPY solardash ./solardash
RUN pip install --no-deps .

VOLUME ["/app/data"]
EXPOSE 8080
ENV SOLARDASH_CONFIG=/app/config.yaml SOLARDASH_DB=/app/data/solardash.db
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status==200 else 1)"
CMD ["solardash", "serve"]
