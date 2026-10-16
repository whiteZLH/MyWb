# 一键式部署镜像：自包含，无需预先构建基础镜像。
# 同时包含协议转换核心、中文管理后台与内置 sing-box（用于账号级 vless:// 出站）。
FROM python:3.12-slim

# 内置 sing-box 版本与下载源；国内构建可从 GitHub 直连失败，用 --build-arg SINGBOX_URL_BASE=<镜像/代理> 覆盖。
ARG SINGBOX_VERSION=1.14.3
ARG SINGBOX_URL_BASE=https://github.com/SagerNet/sing-box/releases/download
# BuildKit 会自动注入 TARGETARCH（amd64 / arm64 / arm / 386 ...）。
ARG TARGETARCH=amd64
ARG TARGETVARIANT=""
# 国内可传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=""

WORKDIR /app

COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then pip install --no-cache-dir -i "$PIP_INDEX_URL" -r requirements.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi

# 按目标架构下载静态 sing-box 到镜像内（Go 静态编译，可直接跑）。
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
