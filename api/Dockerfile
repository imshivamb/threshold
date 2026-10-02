FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# Install dependencies first so code edits don't bust the layer cache.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

COPY . .

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port 8000"]
