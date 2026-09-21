FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends adb tzdata iputils-ping netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 3019 frames && useradd -u 3019 -g 3019 -d /data frames
ENV HOME=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY controller.py webui.py frame_log.py /app/
COPY templates /app/templates
COPY static /app/static
# NAS source files may have restrictive modes; the runtime user needs read/traverse access.
RUN find /app -type d -exec chmod 755 {} + \
    && find /app -type f -exec chmod 644 {} +
EXPOSE 8080
USER 3019:3019
# Fail the build if the runtime user cannot import the application.
RUN python -c "import controller, webui"
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/controller.py", "health"]
CMD ["python", "/app/controller.py", "run"]
