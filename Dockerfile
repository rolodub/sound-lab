# Sound Lab tools (phase 0: inventory). ffprobe for non-WAV/AIFF files; no network needed at run time.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY soundlab ./soundlab
RUN pip install --no-cache-dir .
USER 10001:10001
ENTRYPOINT ["python", "-m", "soundlab"]
