# 管理后台镜像：自包含，无需预先构建基础镜像。
# 内置 sing-box（用于账号级 vless:// 出站）。
FROM python:3.12-slim

ARG SINGBOX_VERSION=1.14.3
ARG SINGBOX_URL_BASE=https://github.com/SagerNet/sing-box/releases/download
ARG TARGETARCH=amd64
ARG TARGETVARIANT=""
ARG PIP_INDEX_URL=""

WORKDIR /app

COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi

RUN set -eux; \
    case "${TARGETARCH}${TARGETVARIANT}" in \
      amd64) arch=amd64 ;; \
      arm64) arch=arm64 ;; \
      armv7) arch=armv7 ;; \
      armv6) arch=armv6 ;; \
      arm) arch=armv7 ;; \
      386) arch=386 ;; \
      *) arch=amd64 ;; \
    esac; \
    url="${SINGBOX_URL_BASE}/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-${arch}.tar.gz"; \
    echo "Downloading ${url}"; \
    mkdir -p /tmp/sb; \
    python3 -c "import urllib.request,sys; urllib.request.urlretrieve(sys.argv[1], '/tmp/sb/sing-box.tgz')" "${url}"; \
    tar -xzf /tmp/sb/sing-box.tgz -C /tmp/sb; \
    find /tmp/sb -type f -name sing-box -exec mv {} /usr/local/bin/sing-box \; ; \
    chmod 0755 /usr/local/bin/sing-box; \
    rm -rf /tmp/sb; \
    sing-box version

COPY core ./core
COPY admin ./admin

EXPOSE 8787

CMD ["python3", "-m", "admin.server"]
