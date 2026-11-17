# OpenTabular test deployment: the explorer, the HTTP API and the example data app.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

# The project to serve. Replace examples/sales with your own project folder.
COPY examples/sales ./project

ENV PORT=10000
EXPOSE 10000
# OTAB_PASSWORD must be set: the server refuses a public address without it.
CMD ["sh", "-c", "otab serve --project /app/project --host 0.0.0.0 --port ${PORT}"]
