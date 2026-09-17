# The read-only web dashboard, as its OWN image so the scheduler image never installs the web
# extras (requirements-web.txt) and the scheduler service is not touched. Built by the `web`
# compose profile: `docker compose --profile web up -d --build`.
#
# It shares the scheduler's runtime requirements because the sheet backend reads through the same
# gspread / google-auth path (scripts.audit_sheet.open_worksheet_readonly), and the settings module
# is the project's own. No browser, no scraper, no buying-group client is ever invoked here.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-web.txt

# App code only -- config.json and the data/logs directories are mounted read-only at runtime
# (see the `web` service in docker-compose.yml); .dockerignore keeps secrets out of the context.
COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8765

# 0.0.0.0 INSIDE the container; the compose file decides which host interface is published
# (127.0.0.1 by default -- see WEB_PUBLISH_HOST there).
CMD ["python", "-m", "web", "--host", "0.0.0.0", "--port", "8765"]
