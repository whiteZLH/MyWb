ARG BASE_IMAGE=local/mywb:f717db6
FROM ${BASE_IMAGE}
WORKDIR /app
COPY core ./core
COPY admin ./admin
CMD ["python3", "-m", "admin.server"]
