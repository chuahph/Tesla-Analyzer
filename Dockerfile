FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 8000

# Runs in DEMO mode by default; provide TESLA_ACCESS_TOKEN for live data.
CMD ["python", "run.py", "serve", "--host", "0.0.0.0", "--port", "8000"]
