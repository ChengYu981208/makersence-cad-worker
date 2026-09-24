FROM python:3.11
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl file xvfb xauth libglib2.0-0 libgtk-3-0 libwebkit2gtk-4.1-0 libdbus-1-3 \
    libfontconfig1 libfreetype6 libx11-6 libxext6 libxrender1 libxi6 libxrandr2 libxinerama1 \
    libxcursor1 libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxkbcommon-x11-0 \
    libgl1 libgl1-mesa-dri libglu1-mesa libegl1 libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
    libsoup-3.0-0 libsecret-1-0 libudev1 fonts-noto-cjk fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.runtime.txt .
RUN python -m pip install --no-cache-dir -r requirements.runtime.txt
RUN curl -fL --retry 3 \
    "https://github.com/bambulab/BambuStudio/releases/download/v02.08.02.61/BambuStudio_ubuntu22.04-v02.08.02.61-20260820225108.AppImage" \
    -o /tmp/BambuStudio.AppImage \
    && chmod +x /tmp/BambuStudio.AppImage \
    && mkdir -p /opt/bambu \
    && cd /opt/bambu \
    && /tmp/BambuStudio.AppImage --appimage-extract >/tmp/bambu_extract.log \
    && test -x /opt/bambu/squashfs-root/AppRun \
    && rm -f /tmp/BambuStudio.AppImage
COPY app.py /app/app.py
ENV BAMBU_BIN=/opt/bambu/squashfs-root/AppRun BAMBU_VERSION=2.8.2.61 PORT=8000
EXPOSE 8000
CMD ["python","/app/app.py"]
