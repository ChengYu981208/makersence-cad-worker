FROM python:3.11
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    blender fonts-noto-cjk fonts-dejavu-core libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.runtime.txt .
RUN python -m pip install --no-cache-dir -r requirements.runtime.txt
COPY app.py /app/app.py
ENV BLENDER_BIN=/usr/bin/blender PORT=8000
EXPOSE 8000
CMD ["python","/app/app.py"]
