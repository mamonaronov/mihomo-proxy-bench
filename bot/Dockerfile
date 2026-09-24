FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py app_version.py downtime.py report_image.py ./

ARG GIT_COMMIT=unknown
ARG GIT_COMMIT_TITLE=unknown
RUN printf 'commit=%s\ntitle=%s\n' "$GIT_COMMIT" "$GIT_COMMIT_TITLE" > /app/.build-git

ENV PYTHONUNBUFFERED=1
ENV APP_GIT_COMMIT=$GIT_COMMIT
ENV APP_GIT_COMMIT_TITLE=$GIT_COMMIT_TITLE
CMD ["python", "-u", "bot.py"]
