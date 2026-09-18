FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends adb tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 568 frames && useradd -u 568 -g 568 -d /data frames
ENV HOME=/data PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY controller.py webui.py /app/
COPY templates /app/templates
COPY static /app/static
EXPOSE 8080
USER 568:568
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/controller.py", "health"]
CMD ["python", "/app/controller.py", "run"]
