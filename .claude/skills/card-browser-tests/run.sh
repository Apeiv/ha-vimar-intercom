#!/usr/bin/env bash
# The card's browser tests (pytest -m browser) in a disposable Playwright
# container. Nothing is installed on the host: the repo is copied into the
# container (so no test writes into the checkout), Python packages and ffmpeg
# are installed there, and the container is removed on exit.
#
#   .claude/skills/card-browser-tests/run.sh                       # all browser tests
#   .claude/skills/card-browser-tests/run.sh -k fill_fit -s        # extra pytest arguments
#   MARK=media .claude/skills/card-browser-tests/run.sh            # the media tests instead
#   ARTIFACTS=/tmp/card-out .claude/skills/card-browser-tests/run.sh  # keep test output here
set -euo pipefail

IMAGE="${PLAYWRIGHT_IMAGE:-mcr.microsoft.com/playwright/python:v1.63.0-noble}"
# The image ships the browsers and their libraries but not the Python package:
# it is installed in the container at the image's own version.
PW_VERSION="$(sed -E 's/.*:v([0-9.]+).*/\1/' <<<"$IMAGE")"
MARK="${MARK:-browser}"
REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
OUT=()
if [[ -n "${ARTIFACTS:-}" ]]; then
  mkdir -p "$ARTIFACTS"
  OUT=(-v "$ARTIFACTS:/out")
fi

exec docker run --rm --init --ipc=host \
  -v "$REPO:/src:ro" "${OUT[@]}" \
  -e MARK="$MARK" -e PW_VERSION="$PW_VERSION" -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" bash -euo pipefail -c '
    cp -a /src /w && cd /w
    # ffmpeg makes the fake panel video for the WebCodecs tests; without it they skip.
    (apt-get update -qq && apt-get install -y -qq ffmpeg) >/dev/null 2>&1 \
      || echo "ffmpeg not installed: the video tests will skip"
    pip install -q --break-system-packages --root-user-action=ignore \
      -r requirements-dev.txt aiohttp "playwright==$PW_VERSION"
    python -c "import aiohttp, playwright" || { echo "test dependencies missing"; exit 2; }
    status=0
    python -m pytest tests -m "$MARK" -p no:cacheprovider -rs "$@" 2>&1 | tee /tmp/pytest.log || status=$?
    [[ -d /out ]] && cp /tmp/pytest.log /out/ && echo "log copied to the artifacts directory"
    exit "$status"
  ' _ "$@"
