FROM python:3.11
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates flatpak dbus dbus-user-session xvfb xauth mesa-utils strace \
    libgl1 libgl1-mesa-dri libglx-mesa0 libosmesa6 fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig \
    && dbus-uuidgen --ensure=/etc/machine-id \
    && mkdir -p /var/lib/dbus /run/dbus \
    && ln -sf /etc/machine-id /var/lib/dbus/machine-id \
    && useradd -m -s /bin/bash slicer \
    && install -d -m 700 -o slicer -g slicer /run/user/$(id -u slicer) \
    && install -d -o slicer -g slicer /home/slicer/.local/share /home/slicer/.local/state /home/slicer/.cache /home/slicer/.config /home/slicer/makersence_slicer_jobs \
    && install -d -m 700 -o slicer -g slicer /tmp/makersence-display
COPY app.py /app/app.py
COPY start.sh /app/start.sh
RUN chmod 755 /app/start.sh
ENV PORT=8080 BAMBU_VERSION=2.8.2.61 BAMBU_DISPLAY_MODE=x11_flatpak SLICER_JOB_ROOT=/home/slicer/makersence_slicer_jobs
EXPOSE 8080
CMD ["/app/start.sh"]
