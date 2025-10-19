# Dockerfile
FROM python:3.12-slim
LABEL authors="akashbasa"
WORKDIR /app


COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir /app

# Copy your user-facing app
COPY app.py /app/app.py

ENV PYTHONUNBUFFERED=1 \
    LOG_LEVEL=INFO \
    LOG_USE_COLOR=1

CMD ["python", "app.py"]
