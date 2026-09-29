FROM python:3.12-slim
WORKDIR /app

# The default index can be slow or unreachable from mainland-China hosts, so the
# index is a build arg. Override at build time with
#   docker compose build --build-arg PIP_INDEX_URL=https://pypi.org/simple/
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

COPY gateway/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir \
      --index-url "${PIP_INDEX_URL}" \
      --trusted-host "${PIP_TRUSTED_HOST}" \
      -r /app/requirements.txt

COPY gateway /app/gateway

# The migration tree needs *both* services' models: they share one PostgreSQL
# database and therefore one schema history, so ``alembic upgrade head`` cannot
# be split across the two images. Only the ``app`` package is copied — the
# business service's Dockerfile and requirements stay out, so the gateway image
# does not grow a second web framework to run a one-shot migration.
COPY business_service/app /app/business_service/app
COPY alembic.ini /app/alembic.ini
COPY migrations /app/migrations

ENV PYTHONPATH=/app
CMD ["uvicorn", "gateway.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
