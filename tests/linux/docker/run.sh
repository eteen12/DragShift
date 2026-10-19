#!/bin/sh
# Run the isolated-session tests inside the Docker image (CI parity for tests/linux).
#
#   tests/linux/docker/run.sh                 # pytest -m gnome tests/linux
#   tests/linux/docker/run.sh -k nautilus -v  # extra arguments go to pytest
#
# Builds the image first (cached layers make this a no-op when nothing changed), then
# runs it with the repo mounted read-only at /src. Software GL (llvmpipe): no /dev/dri,
# no --privileged. DRAGSHIFT_TEST_IMAGE overrides the image tag; DOCKER_BUILD_ARGS adds
# build options (e.g. "--build-arg WITH_LIBREOFFICE=1" or "--no-cache --pull").
set -eu

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../../.." && pwd)
image=${DRAGSHIFT_TEST_IMAGE:-dragshift-gnome-test:local}

# The build context is just the Dockerfile and requirements.txt, sent as a tar on stdin
# (works with BuildKit and the legacy builder, and never ships .venv or .git).
# shellcheck disable=SC2086
tar -cf - -C "$here" Dockerfile -C "$root" requirements.txt \
  | docker build ${DOCKER_BUILD_ARGS:-} -t "$image" -

tty=
[ -t 0 ] && tty="$tty -i"
[ -t 1 ] && tty="$tty -t"
# shellcheck disable=SC2086
exec docker run --rm --init $tty --shm-size=512m \
  --name "dragshift-gnome-test-$$" \
  -v "$root:/src:ro" -w /src \
  "$image" python -m pytest -p no:cacheprovider -m gnome tests/linux "$@"
