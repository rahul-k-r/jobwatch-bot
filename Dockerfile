FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY jobwatch ./jobwatch
RUN pip install --no-cache-dir .
COPY config.yaml ./

ENV JOBWATCH_DB=/data/jobwatch.db PYTHONUNBUFFERED=1
VOLUME /data
CMD ["python", "-m", "jobwatch"]
