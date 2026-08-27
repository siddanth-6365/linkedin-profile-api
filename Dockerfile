FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies resolve from pyproject alone, so this layer caches across source edits.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000

# Shell form so the host's injected $PORT expands (Render defaults it to 10000).
# 0.0.0.0 because Render requires it — a container bound to localhost gets no
# traffic; the :-8000 default keeps plain `docker run -p 8000:8000` working.
CMD ["sh", "-c", "uvicorn linkedin_profile_api.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
