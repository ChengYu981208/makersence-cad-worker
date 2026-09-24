#!/bin/bash
set -euo pipefail

if [ ! -S /run/dbus/system_bus_socket ]; then
  dbus-daemon --system --fork --nopidfile
fi
test -S /run/dbus/system_bus_socket

OSMESA_PATH="$(ldconfig -p | awk '/libOSMesa\.so/{print $NF; exit}')"
test -n "$OSMESA_PATH"

SUID="$(id -u slicer)"
run_slicer_env() {
  runuser -u slicer -- env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME=/home/slicer USER=slicer LOGNAME=slicer SHELL=/bin/bash \
    XDG_DATA_HOME=/home/slicer/.local/share \
    XDG_CACHE_HOME=/home/slicer/.cache \
    XDG_CONFIG_HOME=/home/slicer/.config \
    XDG_STATE_HOME=/home/slicer/.local/state \
    XDG_RUNTIME_DIR="/run/user/$SUID" \
    XDG_DATA_DIRS=/home/slicer/.local/share/flatpak/exports/share:/usr/local/share:/usr/share:/var/lib/flatpak/exports/share \
    XDG_CONFIG_DIRS=/etc/xdg \
    FLATPAK_USER_DIR=/home/slicer/.local/share/flatpak \
    DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket \
    "$@"
}

run_slicer_env dbus-run-session -- flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
run_slicer_env dbus-run-session -- flatpak install --user -y --noninteractive --or-update flathub com.bambulab.BambuStudio
run_slicer_env dbus-run-session -- flatpak info --user com.bambulab.BambuStudio | tee /tmp/bambu_flatpak_info.log
grep -F "2.8.2.61" /tmp/bambu_flatpak_info.log

cat > /usr/local/bin/bambu-direct <<'EOS'
#!/bin/bash
set -euo pipefail
APP_DEPLOY="$(readlink -f /home/slicer/.local/share/flatpak/app/com.bambulab.BambuStudio/x86_64/stable/active)"
RUNTIME_DEPLOY="$(readlink -f /home/slicer/.local/share/flatpak/runtime/org.gnome.Platform/x86_64/50/active)"
APP_FILES="$APP_DEPLOY/files"
RUNTIME_FILES="$RUNTIME_DEPLOY/files"
export HOME=/home/slicer USER=slicer LOGNAME=slicer LC_ALL=C.UTF-8
export PATH="$APP_FILES/bin:$RUNTIME_FILES/bin:/usr/local/bin:/usr/bin:/bin"
TARGET_LD="$APP_FILES/lib:$APP_FILES/lib/x86_64-linux-gnu:$RUNTIME_FILES/lib:$RUNTIME_FILES/lib/x86_64-linux-gnu:$RUNTIME_FILES/lib64"
LOADER=""
for CAND in "$RUNTIME_FILES/lib64/ld-linux-x86-64.so.2" "$RUNTIME_FILES/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"; do
  if [ -x "$CAND" ]; then LOADER="$CAND"; break; fi
done
test -n "$LOADER"
export XDG_DATA_HOME=/home/slicer/.local/share
export XDG_CACHE_HOME=/home/slicer/.cache
export XDG_CONFIG_HOME=/home/slicer/.config
export XDG_STATE_HOME=/home/slicer/.local/state
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export XDG_DATA_DIRS="$APP_FILES/share:$RUNTIME_FILES/share:/usr/local/share:/usr/share"
export XDG_CONFIG_DIRS=/etc/xdg
export GSETTINGS_SCHEMA_DIR="$RUNTIME_FILES/share/glib-2.0/schemas"
export GI_TYPELIB_PATH="$RUNTIME_FILES/lib/x86_64-linux-gnu/girepository-1.0:$RUNTIME_FILES/lib/girepository-1.0"
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
exec /usr/bin/strace -ff -tt -s 256 -o /home/slicer/bambu.strace \
  "$LOADER" --library-path "$TARGET_LD" "$APP_FILES/bin/bambu-studio" "$@"
EOS
chmod 755 /usr/local/bin/bambu-direct

exec runuser -u slicer -- env -i \
  PATH=/usr/local/bin:/usr/bin:/bin \
  HOME=/home/slicer USER=slicer LOGNAME=slicer SHELL=/bin/bash \
  XDG_DATA_HOME=/home/slicer/.local/share \
  XDG_CACHE_HOME=/home/slicer/.cache \
  XDG_CONFIG_HOME=/home/slicer/.config \
  XDG_STATE_HOME=/home/slicer/.local/state \
  XDG_RUNTIME_DIR="/run/user/$SUID" \
  XDG_DATA_DIRS=/home/slicer/.local/share/flatpak/exports/share:/usr/local/share:/usr/share:/var/lib/flatpak/exports/share \
  XDG_CONFIG_DIRS=/etc/xdg \
  FLATPAK_USER_DIR=/home/slicer/.local/share/flatpak \
  DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket \
  PORT="\${PORT:-8080}" \
  SLICER_TOKEN="\${SLICER_TOKEN:-}" \
  BAMBU_BIN=/usr/local/bin/bambu-direct \
  BAMBU_VERSION=2.8.2.61 \
  BAMBU_DISPLAY_MODE=x11_flatpak \
  SLICER_JOB_ROOT=/home/slicer/makersence_slicer_jobs \
  python /app/app.py
