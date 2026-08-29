FROM python:3.11-slim

# System dependencies (see README):
#   libreoffice-impress - converts .pptx -> .pdf (only Impress is used, not the
#                         full office suite; --no-install-recommends also skips
#                         the bundled JRE and extra font/dictionary packs)
#   poppler-utils       - backs pdf2image (pdftoppm)
#   ffmpeg              - builds and concatenates the per-slide video clips
RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-impress \
      poppler-utils \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so this layer is cached across code-only changes.
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

RUN mkdir -p data/jobs \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser
ENV HOME=/home/appuser

EXPOSE 8000

WORKDIR /app/backend
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
