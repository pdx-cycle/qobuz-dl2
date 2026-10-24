# Offline audit evidence

These scripts reproduce observations against the working tree. An exit code of zero means the probe ran; it does **not** mean the defect is fixed. They deliberately exercise broken behavior and are not regression tests to preserve that behavior.

Run each in a separate process from the repository root, using an environment with the project dependencies:

```sh
uv run python docs/superpowers/audits/2026-10-22/evidence/core_probe.py
uv run python docs/superpowers/audits/2026-10-22/evidence/download_probe.py
uv run python docs/superpowers/audits/2026-10-22/evidence/artwork_probe.py
uv run python docs/superpowers/audits/2026-10-22/evidence/api_probe.py
uv run python docs/superpowers/audits/2026-10-22/evidence/cli_probe.py
uv run python docs/superpowers/audits/2026-10-22/evidence/quality_probe.py
uv run python docs/superpowers/audits/2026-10-22/evidence/browser_probe.py
uv run python docs/superpowers/audits/2026-10-22/evidence/collection_benchmark.py
uv run python docs/superpowers/audits/2026-10-22/evidence/stream_benchmark.py
```

All HTTP traffic uses `httpx.MockTransport`; browser evidence uses fake Playwright objects. No Qobuz credentials, personal config, real browser, subscription, or media download is accessed. File/config/SQLite mutations are confined to temporary directories. `core_probe.py` counts descriptors via `/dev/fd`, so that observation requires a compatible Unix host. Streaming benchmarks use a 64 MiB in-memory payload and synthetic latency; they do not measure Qobuz throughput.

The artwork race probe deliberately supplies invalid audio to isolate the current tagger's image dependency. After audio validation is implemented, reproduce the artwork dependency with a valid, locally generated audio fixture.

`observations.txt` records this audit's outputs. `environment.json` identifies the dependency versions used. `source-hashes.json` fingerprints the pre-audit product, tests, README, and package manifests; these were preserved throughout the audit. Dependency setup from the lockfile was unavailable due to package-host DNS failure; the audit used existing local runtime packages and cached development tools instead. Commands above describe the normal reproducible workflow once locked dependencies are available.
