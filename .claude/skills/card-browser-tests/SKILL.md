---
name: card-browser-tests
description: Run the Lovelace card's browser tests (pytest -m browser, Chromium/WebKit via Playwright) or the media tests in a disposable Docker container, without installing anything on the host. Use when changing the card (www/*.js), the card harness (tests/harness/card.py), or when a browser/media test fails in CI.
---

# Card browser tests in a container

The card (`custom_components/vimar_intercom/www/vimar-intercom-card.js`) is tested in real
browsers by `tests/test_e2e_card.py` through Playwright. Those tests are excluded from the
default run (`-m browser`, see `pyproject.toml`) and need Chromium, its system libraries,
aiohttp and, for the WebCodecs video tests, ffmpeg.

**Never install these on the host** (AGENTS.md): no apt packages, no global pip, no
browsers in `~/.cache`. Run them with the script here, which uses a disposable
Playwright container.

## Run

```bash
.claude/skills/card-browser-tests/run.sh                  # every browser test
.claude/skills/card-browser-tests/run.sh -k fill_fit -s   # a subset, with prints
.claude/skills/card-browser-tests/run.sh -x -vv           # stop at the first failure
MARK=media .claude/skills/card-browser-tests/run.sh       # the media tests (real ffmpeg)
ARTIFACTS=/tmp/card-out .claude/skills/card-browser-tests/run.sh   # keep the pytest log
```

Extra arguments go to pytest. `PLAYWRIGHT_IMAGE` overrides the image; keep its Playwright
version equal to the one the tests were written against.

## What it does

1. `docker run --rm` of the official Playwright image (Chromium and WebKit with their
   libraries already inside).
2. Copies the repository into the container read-only, so no test writes into the
   checkout.
3. Installs `requirements-dev.txt`, aiohttp and ffmpeg inside the container.
4. Runs `pytest tests -m browser -rs` and prints why anything was skipped.

## Debugging a failure

- `-k <name> -s` shows the test's prints (latency timelines, for instance).
- The page records `T.errors` (JS errors and unhandled rejections) and `info()` returns
  the card's state; failed `c.until(...)` assertions print both.
- WebKit has no WebCodecs: tests that need the canvas player are Chromium-only
  (`@pytest.mark.parametrize("engine", ["chromium"], indirect=True)`).
- A test skipped with "serve ffmpeg" means ffmpeg did not install in the container.

The first run pulls the image (about 2 GB). `docker image rm <image>` removes it.
