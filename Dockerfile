FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download embedding model at build time
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir -e .

ENV MEMORY_DB_PATH=/data/memory.db
ENV MEMORY_MODEL=all-MiniLM-L6-v2

EXPOSE 8787

ENTRYPOINT ["python", "-m", "claude_memory.server"]
CMD ["--transport", "stdio"]
