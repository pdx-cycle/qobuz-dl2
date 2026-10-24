# Qobuz DL2 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make supported downloads complete accurately, preserve files and settings on failure, and improve throughput only where measured.

**Architecture:** Preserve the Click → core → API/downloader → metadata structure. Add explicit result and artifact contracts, operation-owned staging, dependency-aware tagging, and artifact-backed history. Use a shared bounded transfer budget rather than multiplying per-release concurrency; retain HTTPX, Tenacity, SQLite, and Mutagen.

**Tech Stack:** Python >=3.9, Click, asyncio, HTTPX, Tenacity, Rich, SQLite, Mutagen, pytest, Ruff, mypy, uv, Hatchling.

**Spec:** [2026-10-22 audit and acceptance requirements](../audits/2026-10-22-audit.md).

**Execution status:** Implemented locally after user approval. See the
[qualification and independent-review report](../audits/2026-10-22-remediation.md)
for actual regression coverage, measured performance, decisions, and remaining
environment checks. The numbered steps below preserve the original proposal;
their checkboxes do not assert unperformed locked/platform/live qualification.

## Global Constraints

- Preserve the existing dirty README, CLI, core, downloader, and two untracked tests. Record/reconcile their diffs before edits; do not reset, stash, overwrite, or commit unrelated baseline changes.
- Python >=3.9 remains the declared floor. No floor increase or new mandatory service/browser dependency.
- Keep the existing conservative default limit of **2** until target-network evidence supports another value.
- Existing history and audio must remain preserved during later migrations; legacy history alone cannot establish file validity.
- No real credentials in tests/logs/artifacts; no live downloads or browser login in automated checks. Use user-owned media fixtures or locally generated, redistributable synthetic media.
- Final paths denote validated complete artifacts. Cleanup deletes only files created and owned by the current operation.
- Exit 0 means no failed requested operations; intentional policy skips are distinct from failures and never create history entries. Authentication/transfer/tagging/invalid-input failures exit nonzero; Ctrl-C exits 130 after cleanup.
- Retry only explicitly transient operations within a fixed attempt budget. Do not blindly replay login POST.
- Keep changes scoped to audited failures and measured scheduling improvements; avoid an unrelated rewrite.

## Review Focus

- Cancellation while a tagging executor thread still runs must not race cleanup or publish later without ownership (Tasks 2–3).
- Repeated playlist tracks, changed destination/quality, and stale legacy IDs must produce the requested local artifacts and ordered entries (Tasks 4–5).
- Missing, null, malformed, or mixed-quality API data must yield typed outcomes without false completion (Tasks 1, 3, 7).
- Help, initial token setup, malformed config, interrupted writes, and simultaneous config updates must preserve usable settings (Task 6).
- A large collection must keep active streams and pending work bounded, preserve cancellation, and avoid retrying fatal responses (Tasks 7–8).

## Contracts and file responsibilities

Create `qobuz_dl2/results.py` for immutable results only:

```python
@dataclass(frozen=True)
class Artifact:
    path: str                  # absolute final path
    item_type: str             # album or track identity namespace
    item_id: str
    source_track_id: str       # required even when the request is an album
    requirements_key: str      # normalized rendering/embedding/tag-schema requirements
    format_id: int             # actual API format, not requested fallback
    bit_depth: Optional[int]
    sampling_rate: Optional[float]

class DownloadStatus(Enum):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"

@dataclass(frozen=True)
class DownloadResult:
    status: DownloadStatus
    artifacts: Tuple[Artifact, ...] = ()
    reason: str = ""
    errors: Tuple[str, ...] = ()

@dataclass(frozen=True)
class BatchResult:
    results: Tuple[DownloadResult, ...] = ()
    # property has_failures: bool; true iff any result is FAILED
```

COMPLETED requires at least one verified artifact and satisfaction of the complete current request. An album must enumerate its expected source track IDs from current metadata; every eligible member must have a verified matching artifact, and any policy exclusion must remain an explicit skip. A surviving subset never establishes album completion. Aggregate failure may carry successful siblings. SKIPPED carries a reason and never creates new completion history. Cover/booklet warnings are separate from successful audio completion unless required for requested embedding. Keep exceptions for interrupted/control-flow and transport internals; convert failures once at orchestration boundaries.

Artifact reuse requires matching source identity, destination, actual quality, and `requirements_key` (embedding requested, normalized naming patterns, and tag-schema version). Media validity alone does not prove identity. If a destination contains valid audio with unknown or conflicting provenance, report a collision and preserve it; never silently reuse it as a different song. A valid survivor can satisfy only its own requested member.

Create `qobuz_dl2/storage.py` for unique staging/publication/validation and owned cleanup; `qobuz_dl2/config.py` for validated atomic config persistence; `qobuz_dl2/network.py` for reusable GET retry classification. Keep tagging in metadata, orchestration in core/downloader, migrations in db, and playlist formatting in utils. Do not move unrelated code.

## Task 1: Truthful outcomes and a working collection interface

Addresses A01, A02, and the result foundation for A07–A10.

**Files:** Create `qobuz_dl2/results.py`, `tests/test_results.py`, `tests/test_core.py`, `tests/test_qopy.py`; modify `core.py:75–183`, `qopy.py:190–197`, `downloader.py:105–325`, `cli.py:351–410`; extend existing downloader tests without overwriting their baseline.

**Interfaces:** Metadata getters become ordinary methods returning `AsyncIterator[dict]` from `multi_meta`. `Download.download_id_by_type(track: bool = True) -> DownloadResult`; `QobuzDL.download_from_id(...) -> DownloadResult`; collection/batch download entry points return `BatchResult`. `_run` consumes the batch outcome and maps failures to Click exit 1.

- [ ] Write `test_collection_getters_return_async_iterators` parametrized across artist/playlist/label, using real getters with a mocked `api_call`; each must yield metadata without coroutine warnings. Write `test_album_skip_does_not_write_history`, `test_unavailable_track_does_not_write_history`, `test_tag_failure_propagates_failed_result`, and `test_auth_failure_exit_is_nonzero`.
- [ ] Run `uv run pytest tests/test_results.py tests/test_core.py tests/test_qopy.py tests/test_downloader.py tests/test_cli_config.py -q`; verify the added behavior tests fail on the audited behavior before implementation.
- [ ] Implement the contracts above; remove swallowed fatal tag errors; preserve per-item context without embedding signed URLs/tokens in errors. Make successful, skipped, partial-failure, and authentication paths explicit. Record no empty or failed result as complete.
- [ ] Re-run the focused tests. Add `test_batch_keeps_successful_siblings_but_exits_one`, `test_all_policy_skips_exit_zero_without_completed_log`, and malformed/empty metadata tests. Existing search-only/interactive return behavior must be explicitly preserved or adapted by the CLI, not silently broken.
- [ ] Review the scoped diff and commit only approved changes: `fix: propagate download outcomes and repair collection iteration`.

## Task 2: Own staging files, validate media, and publish atomically

Addresses A04, A05 and A15; depends on Task 1.

**Files:** Create `qobuz_dl2/storage.py`, `tests/test_storage.py`, `tests/fixtures/README.md`, valid synthetic audio fixtures with provenance; modify `downloader.py:270–325, 394–463`, `metadata.py:123–244`, `cli.py:105–111, 376–378`.

**Interfaces:** `validate_audio(path: str, is_mp3: bool) -> None` raises a typed validation error. `StagedArtifact(final_path: str)` owns a unique same-directory temporary path, exposes `path: str`, `publish() -> str`, and `cleanup() -> None`; publication is explicit after validation/tagging. Tagging mutates only staging paths and does not rename them. Owned cleanup is idempotent. `_stream_to_file` writes the supplied staging path and validates representation-aware byte counts.

- [ ] Write `test_html_or_empty_mp3_is_never_published`, `test_truncated_audio_is_rejected`, `test_valid_audio_is_published_after_tagging`, `test_cancelled_extra_leaves_no_final`, `test_unrelated_tmp_survives_cleanup`, `test_two_operations_have_distinct_staging_paths`, and `test_encoding_aware_transfer_integrity`. Use real Mutagen plus valid locally generated fixtures; do not assert that arbitrary bytes are audio.
- [ ] Run `uv run pytest tests/test_storage.py tests/test_downloader.py tests/test_metadata.py -q`; confirm the new defect-specific tests fail before the fix.
- [ ] Implement unique file ownership and atomic same-filesystem publication. Reject obvious nonaudio and define duration/size/frame validation limits for truncation detection; never describe a header parse as full audio integrity. Compare raw transfer count with encoded length, or explicitly request/validate identity representation; write decoded output coherently. Preserve truncation detection.
- [ ] Replace broad recursive temp cleanup with tracked owned files. On errors/cancellation, settle each writer before cleanup. Ensure final destinations are not silently overwritten by competing processes; reuse only an artifact whose provenance and requirements match, otherwise report a collision and preserve the existing file. Use cancellation-safe completion ownership around executor tagging before any publication.
- [ ] Run focused tests plus `test_disk_full_preserves_existing_final`, `test_cancel_during_tagging_cannot_publish_after_return`, `test_existing_invalid_audio_is_not_trusted`, `test_distinct_tracks_same_custom_path_report_collision`, and `test_tag_failure_removes_only_owned_staging`. Verify no `.tmp`, partial final, or background writer survives test completion.
- [ ] Review and commit: `fix: validate and atomically publish owned download artifacts`.

## Task 3: Complete artwork dependencies and per-track quality policy

Addresses A03 and A19; depends on Tasks 1–2.

**Files:** Modify `downloader.py:105–253, 358–391`, `metadata.py:69–119`; extend `tests/test_downloader.py`, `tests/test_metadata.py`.

**Interfaces:** Metadata embedders consume an explicit completed `cover_path: Optional[str]`; remove directory-probing fallbacks. Downloaded audio may overlap cover fetching, but embedding waits on the completed artwork task. `_process_track` evaluates its own file URL restrictions and actual format before transfer/publication, producing `DownloadResult`.

- [ ] Write `test_embedding_waits_for_cover_creation`, `test_embedding_waits_for_cover_flush`, and `test_multidisc_embedding_uses_completed_album_cover` with event-gated streams and real valid audio. Assert exact embedded bytes equal the final cover. Write `test_second_track_downgrade_respects_no_fallback`.
- [ ] Run `uv run pytest tests/test_downloader.py tests/test_metadata.py -q`; verify failures reproduce A03/A19.
- [ ] Implement the dependency and quality checks. Missing optional art with embedding disabled is a warning; requested embedding with unavailable/invalid art fails that item's requested result rather than falsely claiming the requested embedding succeeded. Define `--no-cover` plus `--embed-art` as fetching validated art for embedding without retaining the cover afterward; document it.
- [ ] Test cover 404, missing image metadata, oversized image, mixed-quality albums, unavailable first track, empty album, and null optional fields. Assert no album-level quality label substitutes for actual per-track validation.
- [ ] Review and commit: `fix: await complete artwork and enforce quality per track`.

## Task 4: Consume complete collections and generate ordered playlists

Addresses A06, A08, A16, A17 and minor utility boundaries; depends on Task 1 and artifact paths from Task 2.

**Files:** Modify `core.py:127–220, 356–392`, `utils.py:37–73, 76–189`, `qopy.py:171–183`; extend `tests/test_core.py`, `tests/test_utils.py`, `tests/test_qopy.py`.

**Interfaces:** `make_m3u(pl_directory: str, artifacts: Sequence[Artifact]) -> str` uses caller-provided source order. Collection page consumption yields every item; deduplicate album/artist release identities while preserving first appearance, and preserve repeated playlist occurrences. URL parsing retains `get_url_info(url: str) -> Tuple[str, str]` but validates complete host/path structures. Use parsed host for Last.fm dispatch.

- [ ] Write `test_collection_501_items_downloads_every_page`, `test_empty_page_does_not_loop`, `test_playlist_order_and_duplicates_survive`, `test_m3u_root_album_disc_paths_resolve`, and `test_flac_in_mp3_named_directory_uses_flac_parser`.
- [ ] Add the three exact smart-filter cases from `core_probe.py`, null quality values, and all-ineligible groups. Eligible candidates are selected before ranking; prefer remasters among eligible editions, then rank bit depth/rate consistently within that pool. Preserve `save_space` and `skip_extras` behavior explicitly.
- [ ] Add URL tests for foreign hosts, embedded substrings, query/fragment, trailing garbage, malformed IDs, and existing documented relative URL forms. Add `test_recursive_txt_include_is_rejected` and `test_duration_over_24_hours_does_not_wrap`.
- [ ] Run `uv run pytest tests/test_core.py tests/test_utils.py tests/test_qopy.py -q` and confirm each new behavioral assertion fails before its corresponding fix.
- [ ] Implement all-page traversal with progress/termination checks, a cycle guard for text input, filter/ranking corrections, strict URL parsing, relative playlist paths, actual suffix detection, and atomic M3U writing. Write only successful audio entries and report omitted failed entries through `BatchResult`.
- [ ] Re-run focused tests and a 1,501-item synthetic collection. Confirm every intended artifact/entry appears exactly according to source duplicate policy and every M3U path resolves from its own directory.
- [ ] Review and commit: `fix: preserve full collections and ordered playlist artifacts`.

## Task 5: Artifact-backed history and deterministic SQLite lifecycle

Addresses A07, A18; depends on Tasks 1–2 and coordinates with Task 4.

**Files:** Modify `db.py`, `core.py:60–72, 95–125`; create `tests/test_db.py`; extend `tests/test_core.py`.

**Interfaces:** Keep the existing `downloads` table intact as legacy history. Add a versioned artifact table with item type/ID, source track ID, canonical destination, requirements key, actual format/bit depth/rate, size, and completion timestamp. `record_artifacts(db_path: str, artifacts: Sequence[Artifact]) -> None`; `find_valid_artifacts(db_path: str, item_type: str, item_id: str, destination: str, requested_quality: int, requirements_key: str) -> Tuple[Artifact, ...]`. This returns reusable members, not proof of album completion: compare them against the current expected-track manifest before deciding which members still require work. Explicitly close connections; DB exceptions are surfaced, not hidden.

- [ ] Write `test_legacy_ids_are_preserved_but_not_trusted`, `test_second_playlist_destination_materializes`, `test_quality_upgrade_is_not_suppressed`, `test_deleted_or_corrupt_file_is_recovered`, and `test_album_and_track_ids_are_namespaced`. Reuse a valid artifact in its destination without retransferring it; a new destination must receive a verified local artifact before being counted complete.
- [ ] Add `test_connection_closed_after_success_and_error`, `test_migration_failure_preserves_original`, `test_locked_db_reports_failure`, `test_partial_release_records_only_successful_artifacts`, `test_partial_album_retry_fetches_missing_members_only`, and `test_same_quality_embedding_upgrade_is_not_skipped`. A changed embedding request may retag a verified existing artifact atomically rather than redownload it, but must verify the requested result before recording the new requirements key.
- [ ] Run `uv run pytest tests/test_db.py tests/test_core.py -q` to establish failures.
- [ ] Implement additive transactional migration with a local rollback copy before first schema change. Persist only finalized artifacts; validate existence, media, and quality on reuse. Do not treat numeric format ID comparison alone as quality ordering. Use `CREATE TABLE IF NOT EXISTS` for expected existence, explicit schema versioning for migration, and bounded lock waiting.
- [ ] Re-run tests, run 1,000 lookups with GC disabled, and confirm descriptor count stays bounded. Verify original legacy rows and unrelated files remain intact after successful and injected-failure migrations.
- [ ] Review and commit: `fix: back completion history with validated destination artifacts`.

## Task 6: Reliable CLI defaults, maintenance commands, and config persistence

Addresses A09–A11 and CLI portions of A02/A05; depends on Task 1.

**Files:** Create `qobuz_dl2/config.py`, `tests/test_config.py`; modify `cli.py:25–236, 239–505`; extend existing `tests/test_cli_config.py`; update relevant README options.

**Interfaces:** A validated config model provides existing keys to CLI construction. `write_config_atomic(path: str, cfg: ConfigParser) -> None` writes same-directory staging, flushes, atomically replaces, and restricts POSIX mode to 0600; parent directory 0700 on creation. A context-managed advisory lock serializes config read-modify-write with bounded wait on supported platforms. `_save_token` updates the historical field for compatibility, using interpolation-free token storage.

- [ ] Write real CliRunner tests with every saved boolean true and no flags; assert saved values remain effective. Parametrize explicit flags and defaults. Add `test_root_reset_purge_show_config_work_without_subcommand`, `test_help_never_bootstraps_config`, `test_set_token_initializes_without_prompts`, and `test_invalid_options_fail_before_network_or_directory_creation`.
- [ ] Add malformed INI/UTF-8/boolean/range cases, quality choices 5/6/7/27, positive search counts/timeout/concurrency, whitespace-only tokens, invalid patterns, concurrent token/config update, and failed writes preserving original bytes. Assert mode 0600 on POSIX and redacted show-config output.
- [ ] Run `uv run pytest tests/test_cli_config.py tests/test_config.py -q`; verify new assertions fail against baseline.
- [ ] Implement a true absent-flag state, root actions without a dummy subcommand, side-effect-free help, validated configuration diagnostics, and atomic serialized config writes. Keep positional set-token compatibility if needed but add hidden prompt/stdin input and update examples to avoid command-history leakage. Do not automatically overwrite malformed settings or reset them on transient bundle failure.
- [ ] Run focused tests plus Ctrl-C → 130 and aggregate failure → 1 end-to-end CliRunner tests. Confirm token output is redacted and ordinary help/login setup works with no existing config and no live network in tests.
- [ ] Review and commit: `fix: honor CLI configuration and preserve atomic private settings`.

## Task 7: HTTP ownership, schema validation, and classified retries

Addresses A12–A14, A21–A22; depends on Task 1.

**Files:** Create `qobuz_dl2/network.py`, `tests/test_network.py`, `tests/test_browser_login.py`; modify `qopy.py`, `bundle.py`, `browser_login.py`, `exceptions.py`, `core.py:75–89`, `cli.py:232–236`; extend `tests/test_qopy.py`, `tests/test_bundle.py`.

**Interfaces:** `is_retryable_get_error(error: BaseException) -> bool` accepts connection/read timeouts, remote protocol errors, 408, 429, 500, 502, 503, 504. GET retry budget: at most 3 attempts, exponential delay starting at 1 second with jitter, maximum normal delay 10 seconds; valid Retry-After seconds/date is honored up to 60 seconds and remains cancellable. Final error preserves its typed cause. No automatic auth POST replay.

- [ ] Write MockTransport tests for 503→200, timeout→200, 429 Retry-After seconds/date, retry exhaustion, fatal 400/401/403/404, malformed retry headers, and cancellation while waiting. Inject the delay/clock; tests must not actually wait tens of seconds.
- [ ] Add initialization/auth/cfg-setup failure and cancellation cases asserting client closure, malformed login JSON/schema cases, non-string refreshed token, optional membership absent, free-account fixture, and non-JSON secret rejection followed by a valid second candidate. Confirm fatal authorization errors are not misclassified as bad candidate secrets.
- [ ] Add a full synthetic valid bundle fixture plus missing components, invalid Base64/UTF-8, empty decoded secret, no seed, changed script link. Add fake-Playwright tests for intended origin/path, third-party URL, HTTP500, blank/non-string token, timeout and closed window.
- [ ] Run `uv run pytest tests/test_network.py tests/test_qopy.py tests/test_bundle.py tests/test_browser_login.py -q`; establish failing assertions before changes.
- [ ] Implement explicit initialization ownership and typed response validation before token persistence; avoid raw body/signature/credential inclusion in errors. Apply classified GET retry to API/bundle/download operations, preserve cancellation, and validate browser response boundaries. Endpoint allowlists must be grounded in the existing configured API origin, not invented hosts.
- [ ] Run focused tests and confirm no response/client remains open after success, exhaustion, malformed data, or cancellation. Check all error/log outputs with synthetic token sentinels for accidental disclosure.
- [ ] Review and commit: `fix: validate auth responses and bound transient network retries`.

## Task 8: Bound operation-wide scheduling and measure throughput

Addresses A20 and performance aspects of A06/A14/A18; depends on Tasks 1–7.

**Files:** Modify `core.py` collection/Last.fm loops, `downloader.py:42–103, 179–207`, client ownership boundaries; create `tests/test_scheduling.py`, `benchmarks/download_pipeline.py`; document performance methodology in README.

**Interfaces:** One operation owns a reusable download `httpx.AsyncClient` and `asyncio.Semaphore(download_limit)`. `Download` can consume that shared client/budget; retain convenient standalone behavior for library callers by owning/closing its own context only when no shared one is provided. Use a bounded work queue with at most `2 * download_limit` pending jobs; API calls retain their own connection/retry constraints. Artist filtering may retain normalized metadata for ranking, but must not spawn unbounded tasks.

- [ ] Write `test_playlist_reaches_configured_global_concurrency`, `test_multiple_albums_do_not_multiply_transfer_budget`, `test_pending_work_is_bounded`, `test_cancel_stops_producer_workers_and_closes_pool`, and `test_single_track_playlist_reuses_pool`. Active audio transfers must never exceed the configured limit; cover transfers must also respect the shared transport budget.
- [ ] Run `uv run pytest tests/test_scheduling.py -q`; confirm the playlist concurrency assertion fails on baseline serial behavior.
- [ ] Implement shared lifecycle and bounded scheduling while preserving source order in result assembly. Coalesce concurrent occurrences of the same source/destination/requirements into one owned transfer and fan its successful artifact out to every source occurrence; retain repeated M3U entries. Add `test_concurrent_repeated_track_transfers_once_and_emits_each_entry`. Keep default 2. Cancel and await producers, workers, and tagger ownership before pool closure. Preserve retry attempt budgets and do not add range-resume without separate evidence and validator tests.
- [ ] Benchmark limits 1/2/4 with small and large synthetic payloads, delayed/chunked streams, slow tagging/disk simulation, mixed failures, and 1,501 items. Record request/byte counts, connection/pool creations, elapsed time, memory/pending count, event-loop heartbeat, and cancellation latency. Compare medians over five runs; do not enforce fragile wall-clock CI thresholds.
- [ ] Require overlap for independent playlist items and stable bounded memory with exact concurrency/request assertions. If chunk tuning lacks material evidence, keep the existing chunk setting. Live CDN qualification requires a separately authorized run with the user's account; label synthetic evidence accurately.
- [ ] Review and commit: `perf: share bounded transfer scheduling across collection items`.

## Task 9: Package, supported-runtime, and regression qualification

**Files:** Add `.github/workflows/ci.yml` if the hosting target supports GitHub Actions; update `pyproject.toml` test/tool config only where needed, README, fixture provenance, benchmark instructions, and audit follow-up results. Keep development dependencies compatible with the 3.9 matrix; do not silently raise the product floor to accommodate newer tools.

- [ ] In a network-capable environment, run `uv sync --frozen`; repeat all checks using the actual lockfile. Resolve any interpreter-specific dependency issues without substituting newer cache packages and calling the result locked qualification.
- [ ] Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy qobuz_dl2`, and `uv build`. Limit formatting changes to touched code or a separately reviewed formatting commit. Test 3.9 and the current supported interpreter in isolated environments; add Linux/Windows file/config/SQLite checks and macOS coverage where runners are available.
- [ ] Add installed-wheel smoke tests in a fresh temp environment: package import, console entry point, root/subcommand help with no config, invalid options, synthetic token management, and fake-network download producing validated artifacts and correct exit codes. Ensure wheels/sdists contain no context DB, logs, credentials, temp output, or cached media.
- [ ] Link every audit finding A01–A22 to a regression assertion, and record any remaining limits such as header-only media validation or unavailable live browser/CDN evidence. Run the old audit probes only as historical diagnostics; do not make their old bad outputs required CI behavior.
- [ ] Review all user baseline diffs, artifact/history migrations, README behavior, help text, typed errors, logs and rollback instructions. Preserve original files and DB backups; do not purge history as a migration shortcut.
- [ ] Commit approved validation/docs/CI changes, then perform whole-change review with fresh verification evidence. Report local test results separately from hosted CI and any authorized live-client qualification.

## Self-review and execution handoff

Audit coverage: Task 1 → A01/A02; Task 2 → A04/A05/A15; Task 3 → A03/A19; Task 4 → A06/A08/A16/A17; Task 5 → A07/A18; Task 6 → A09/A10/A11; Task 7 → A12/A13/A14/A21/A22; Task 8 → A20; Task 9 qualifies the complete result. All five review-focus classes have explicit test owners above.

Execution used sequential native implementation and one fresh independent
whole-change review. The follow-up report records completion evidence and limits;
the original proposal above remains as planning provenance.
