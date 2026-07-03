FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Stamp the image so the dashboard can show when this build was made.
RUN date -u +%s > /app/.build_time

RUN mkdir -p /app/data

EXPOSE 8000

# Runs in DEMO mode by default; provide TESLA_ACCESS_TOKEN for live data.
# No --port flag: the app binds $PORT when a cloud host sets it (fallback 8000).
CMD ["python", "run.py", "serve", "--host", "0.0.0.0"]
