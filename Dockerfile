# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Hugging Face Spaces requires the app to listen on port 7860
ENV PORT=7860
ENV HOST=0.0.0.0
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ── System dependencies ────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ──────────────────────────────────────────────────────────
WORKDIR /app

# ── Install Python dependencies ────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Copy project files ─────────────────────────────────────────────────────────
COPY main.py .
COPY database.py .
COPY time_norm.py .
COPY static/ ./static/

# ── Copy model checkpoint ──────────────────────────────────────────────────────
# On Hugging Face Spaces, upload your checkpoint folder to the repo
COPY checkpoint-3270/ ./checkpoint-3270/

# ── Expose port ────────────────────────────────────────────────────────────────
EXPOSE 7860

# ── Start the app ──────────────────────────────────────────────────────────────
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
