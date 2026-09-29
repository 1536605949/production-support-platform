FROM python:3.12-slim
WORKDIR /app

# See gateway/Dockerfile — the index is a build arg so this image builds at a
# usable speed from a mainland-China host without pinning the repo to a mirror.
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com

COPY business_service/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir \
      --index-url "${PIP_INDEX_URL}" \
      --trusted-host "${PIP_TRUSTED_HOST}" \
      -r /app/requirements.txt

COPY business_service /app/business_service
ENV PYTHONPATH=/app
CMD ["uvicorn", "business_service.app.main:app", "--host", "0.0.0.0", "--port", "9000"]
