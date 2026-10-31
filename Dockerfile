# One image for every role: `chapterwise serve` (default), `chapterwise worker`, `chapterwise migrate`.

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim
ARG VERSION=dev
LABEL org.opencontainers.image.source="https://github.com/aminghuf/chapterwise" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHAPTERWISE_BOOKS_DIR=/books
RUN useradd --uid 1000 --create-home app
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels
USER app
WORKDIR /home/app
EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=4)"]
CMD ["chapterwise", "serve", "--host", "0.0.0.0", "--port", "8100"]
