# Isolated GNOME Shell 46 session for CI parity (tests/linux/session.py runs inside).
# Build and run with tests/linux/docker/run.sh; its build context holds only this file and
# requirements.txt.
FROM ubuntu:24.04

ARG UV_VERSION=0.11.2
ARG WITH_LIBREOFFICE=0
ENV DEBIAN_FRONTEND=noninteractive

# Session: shell, Xwayland, Files, Ubuntu's default extensions, AT-SPI, software GL.
# yaru-theme-gnome-shell holds the theme --mode=ubuntu loads (the shell dies at boot without
# it); python3-gi runs tests/linux/fake_systemd.py; dconf-cli seeds settings before boot;
# x11-utils gives xdpyinfo. The second group is the apt list from docs/LINUX-DEV.md.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gnome-shell mutter xwayland nautilus dbus dbus-x11 \
      gnome-shell-extension-desktop-icons-ng gnome-shell-extension-ubuntu-dock \
      gnome-shell-extension-appindicator yaru-theme-gnome-shell \
      at-spi2-core gir1.2-atspi-2.0 python3-gi \
      libgl1-mesa-dri mesa-utils fonts-ubuntu fonts-noto-core libxcb-cursor0 \
      dconf-cli x11-utils ca-certificates curl \
      python3-venv libgl1 libegl1 libfontconfig1 libfreetype6 libglib2.0-0t64 \
      libdbus-1-3 libx11-6 libxkbcommon0 libgssapi-krb5-2 \
      libx11-xcb1 libxcb1 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
      libxcb-render0 libxcb-render-util0 libxcb-shape0 libxcb-shm0 libxcb-sync1 \
      libxcb-util1 libxcb-xfixes0 libxcb-xkb1 libxkbcommon-x11-0 \
      libwayland-client0 libwayland-cursor0 libwayland-egl1 \
 && if [ "$WITH_LIBREOFFICE" = 1 ]; then \
      apt-get install -y --no-install-recommends libreoffice-writer-nogui; fi \
 && rm -rf /var/lib/apt/lists/*

# uv from the official installer, pinned.
RUN curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" \
      | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh \
 && uv --version

# Python deps live in the image: the repo is mounted read-only at run time.
COPY requirements.txt /opt/requirements.txt
RUN uv venv --python /usr/bin/python3 /opt/venv \
 && VIRTUAL_ENV=/opt/venv uv pip install --no-cache -r /opt/requirements.txt pytest

# Mutter wants /tmp/.X11-unix to be root-owned with the sticky bit.
RUN mkdir -m 1777 /tmp/.X11-unix \
 && useradd --create-home --shell /bin/bash tester

USER tester
WORKDIR /src
ENV PATH=/opt/venv/bin:$PATH \
    VIRTUAL_ENV=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-m", "gnome", "tests/linux"]
