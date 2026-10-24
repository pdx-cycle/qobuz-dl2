# qobuz-dl (modernized fork) · v2.0.0

Search, explore, and download Lossless and Hi-Res music from [Qobuz](https://www.qobuz.com/).

> **Current service compatibility (October 2026):** Token authentication and
> catalog access still work, but Qobuz retired the legacy `track/getFileUrl`
> endpoint used by this release. Downloads are therefore unavailable even after
> a successful Studio login. Qobuz's replacement uses session-based segmented,
> encrypted CMAF streaming and requires a new transport implementation. Resetting
> the token or application secrets does not repair this service-side break.

> **This is a community fork.** It is a modernized, async rewrite of the original
> [`qobuz-dl` by vitiko98](https://github.com/vitiko98/qobuz-dl), which is no
> longer maintained. All credit for the original tool, the Qobuz API wrapper
> (`qopy`, originally by **Sorrow446**), and the app-secret extraction
> (`spoofbuz`, based on **DashLt**'s work) goes to those authors. This fork is
> free and open source under the **GNU GPL v3** and focuses on getting the tool
> *working again* on today's Qobuz, plus a broad code-quality overhaul.

---

## Why this fork exists

In 2024–2025 Qobuz **removed the direct email/password login API** and moved
authentication behind an OAuth flow protected by reCAPTCHA. The original
`qobuz-dl` (unmaintained since ~2022) logged in with email + password and simply
stopped working — every run failed at login.

**This fork fixes that** by authenticating with a `user_auth_token` taken from a
logged-in browser session instead of a password. You no longer log in from
Python at all; you reuse the session your browser already established.

The token is a short-lived JWT, so the fork also:

- **Auto-refreshes** the token on every run (via the Qobuz `partner` endpoint)
  and writes the fresh token back to your config — so as long as you run the
  tool periodically, you rarely need to touch it again.
- Ships a **browser login helper** (`qobuz-dl2 login`) that captures the token
  for you, and a **`qobuz-dl2 set-token`** command for pasting one manually.

## Features

- **Token-based auth** that works with current Qobuz, with automatic refresh.
- **Fast, concurrent downloads** via an async `httpx` architecture.
- **Resilient networking**: transient download failures retry with exponential
  backoff (`tenacity`) at the network level.
- **Modern CLI** built on `Click` with `rich` progress bars.
- **Download modes**: `dl` (by URL/text file), `fun` (interactive), `lucky`
  (top search result).
- **Smart extras**: multi-disc albums, M3U playlist generation, extended FLAC/MP3
  tagging, cover art embedding, duplicate-download database, and an artist
  "smart discography" filter.

## Requirements

- An **active Qobuz subscription** (free accounts cannot download).
- Python ≥ 3.9.

## Installation

This fork is installed **from source** (it is intentionally not published to
PyPI under the original `qobuz-dl` name). Clone the repo, then install with
[uv](https://docs.astral.sh/uv/) from the project directory:

```bash
git clone <your-fork-url> qobuz-dl2
cd qobuz-dl2

# Core tool — exposes the `qobuz-dl2` command
uv tool install .

# Optional: enable the browser login helper (qobuz-dl2 login)
uv tool install --force ".[browser]"
python -m playwright install chromium   # one-time browser download
```

Start with `qobuz-dl2 login` or `qobuz-dl2 set-token`. Help never creates settings
or contacts Qobuz. Downloads create default settings if needed and give a token
setup message when authentication has not been configured.

## Getting your token

Pick whichever is easiest:

### Option A — automatic (recommended)

```bash
qobuz-dl2 login
```

A browser window opens. **Log in to Qobuz normally** (type your password, solve
any captcha — you do it, not a script). The window closes and your token is
saved automatically. Existing application credentials are preserved; changing a
user token does not prove that the web player's application credentials rotated.
Requires the `[browser]` extra above. A successful login currently establishes
authentication and catalog access; it does not restore the retired download
endpoint described above.

### Option B — paste it manually

1. Log in at <https://play.qobuz.com>.
2. Open **DevTools → Network**, filter by `login`.
3. Click the `user/login` request → **Response** tab.
4. Copy the value of `user_auth_token`.
5. Store it:

   ```bash
   qobuz-dl2 set-token
   ```

The prompt hides your token. `set-token --stdin` also accepts standard input;
the historical positional argument remains supported. If your token expires,
re-run `qobuz-dl2 login` (or `set-token`).

## Usage

```bash
# Download by URL (album, track, artist, label, playlist, or a last.fm playlist)
qobuz-dl2 dl https://open.qobuz.com/album/xxxxxxxxxxxxx

# Download several at once, or from a text file of URLs
qobuz-dl2 dl urls.txt https://open.qobuz.com/track/123456

# Interactive search-and-pick
qobuz-dl2 fun

# Grab the top search result
qobuz-dl2 lucky "miles davis kind of blue"
```

### Quality levels

| ID | Quality                  |
|----|--------------------------|
| 5  | MP3 320                  |
| 6  | 16-bit / 44.1 kHz (CD)   |
| 7  | 24-bit / ≤ 96 kHz        |
| 27 | 24-bit / > 96 kHz        |

```bash
qobuz-dl2 dl -q 27 <url>          # request best Hi-Res
qobuz-dl2 dl --no-fallback -q 27 <url>  # skip tracks unavailable at that quality
```

### Download concurrency

The default is conservative: up to two tracks download at the same time. If
large Hi-Res streams are being closed by the server, try one at a time:

```bash
qobuz-dl2 dl --download-limit 1 -q 27 <url>
```

Run `qobuz-dl2 <command> --help` for the full option list (directory, naming
formats, cover art, database, smart discography, etc.).

## Configuration

Config lives at:

- Linux/macOS: `~/.config/qobuz-dl/config.ini`
- Windows: `%APPDATA%\qobuz-dl\config.ini`

`qobuz-dl2 -r` recreates it, `qobuz-dl2 -sc` prints a redacted copy, and
`qobuz-dl2 -p` purges download history. These commands work without a subcommand.
Saved true flags remain effective when omitted on the command line. Config writes
are atomic and serialized; new config files use mode 0600 on POSIX. Your token is
stored in the historical `password` field in plain text.

### Completion, reuse, and recovery

Downloads stage, validate, and tag audio before atomically publishing it. A failed
track makes the command exit nonzero while successful sibling tracks survive.
Policy exclusions are skips; Ctrl-C exits 130 after owned writers settle. An atomic
commit already in progress may finish before cancellation returns.

History records destination, source identity, physical quality, tags/artwork
requirements, and file digest. Legacy ID rows alone never suppress a download.
The first additive history migration retains a `.pre-artifacts-v1.bak` database.
Retries reuse verified survivors and fetch missing members. Unknown/conflicting
audio at a requested filename is preserved and reported as a collision. Known
audio replaced for corruption, quality, or tagging changes retains its original
bytes in a `.previous-<uuid>` file. These backups are not removed automatically.

Requested embedding waits for validated artwork. `--no-cover --embed-art` fetches
art transiently for embedding without retaining a new cover. Cover cache sidecars
store source hashes and payload digests, never signed URLs or tokens. MP3 checks
include frame boundaries and expected duration; FLAC checks metadata and nonempty
frame data, without decoding every frame. Publication requires a filesystem that
supports hard links; unsupported filesystems report failure while preserving files.

Collection pages are consumed fully. M3U entries follow source order, retain
repeats, and reference actual final paths. One reusable download pool and shared
transfer budget cover tracks and artwork. Each bounded scheduling layer queues at
most twice its worker limit; nested album queues use O(limit²) pending metadata.
GET retries allow at most three attempts for transient errors/statuses; login POST
is not automatically replayed. The default concurrency remains 2.

## Development

```bash
uv sync --frozen
uv run --frozen pytest -q
uv run --frozen ruff check qobuz_dl2 tests scripts benchmarks
uv run --frozen mypy qobuz_dl2
uv build
uv run --frozen python scripts/verify_distributions.py dist
uv run --isolated --no-project --with ./dist/qobuz_dl2-2.0.0-py3-none-any.whl python scripts/smoke_installed.py tests/fixtures/tone.mp3
uv run --frozen python benchmarks/download_pipeline.py --runs 5 --large-collection --output benchmark.json
```

Tests forbid real HTTP transports and isolate config/history. The wheel smoke
uses synthetic token handling and mock HTTP with real audio tagging/publication.
CI covers Python 3.9/3.14 on Linux, macOS, and Windows. Benchmark medians compare
limits 1/2/4 with generated audio, chunk delays, slow tagging, and fatal responses;
they are synthetic measurements, with no wall-clock pass threshold or CDN claim.
See the [audit follow-up](docs/superpowers/audits/2026-10-22-remediation.md) for
finding coverage, measured results, and outstanding environment qualification.

## What changed in this fork (v2.0.0)

- **Auth:** replaced dead email/password login with `user_auth_token` auth +
  automatic refresh and config write-back; added `login` and `set-token`.
- **Bug fixes:** corrected a broken color f-string, an unhandled invalid-URL
  error, an over-eager album-level retry that re-downloaded whole albums, a
  filename-truncation bug that could mangle paths, a `KeyError` on missing MP3
  copyright, a crash in `smart_discography`, and a swapped ℗/© copyright symbol.
- **Robustness:** request timeouts, hardened secret extraction, graceful auth
  errors, and proper HTTP-client cleanup.
- **Maintainability:** de-duplicated the CLI commands, removed dead code,
  dropped the `colorama` dependency, and added a `pytest` smoke suite.

## Credits

- This modernized fork is maintained by **4themusic**.
- Original **qobuz-dl** by [vitiko98](https://github.com/vitiko98/qobuz-dl).
- `qopy` Qobuz API wrapper originally by **Sorrow446**.
- `spoofbuz` app-secret logic based on **DashLt**'s work.

## License

Free and open source under the **GNU General Public License v3.0 or later**
(GPL-3.0-or-later). See [LICENSE](LICENSE) for the full text. You are free to
use, study, share, and modify it; derivative works must remain under the same
license.

This tool is intended for downloading music you are entitled to through your own
Qobuz subscription.
