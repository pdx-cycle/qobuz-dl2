# Qobuz DL2 remediation and qualification

Repairs authorized by the user on October 22, 2026. This follows the point-in-time
[audit](2026-10-22-audit.md) and [implementation plan](../plans/2026-10-22-qobuz-remediation.md).
Baseline HEAD: `430a7f1ddf267c07779bff7387ac474e9d1305f9`. Existing README, CLI,
core, downloader, and two test-file edits were preserved and extended.

## Repair coverage

| Finding | Result and regression evidence |
| --- | --- |
| A01 collection iterator | Plain collection getters return async iterators; `test_collection_getters_return_async_iterators`. |
| A02 false completion | Typed completion/skip/failure, failed aggregate exit, successful sibling artifacts; `test_tag_failure_propagates_failed_result`, `test_failed_outcome_cannot_be_aggregated_as_complete`, `test_auth_failure_exit_is_nonzero`. |
| A03 incomplete artwork | Explicit completed operation-owned cover dependency; `test_embedding_waits_for_complete_cover` gates creation/flush and compares embedded bytes. |
| A04 nonaudio published | Real media validation before publication; `test_html_or_empty_mp3_is_never_published`, `test_truncated_audio_is_rejected`. |
| A05 unsafe ownership | Unique staging, no-overwrite publication, collision checks, settled cancellation; `test_two_operations_have_distinct_staging_paths`, `test_cancel_during_tagging_cannot_publish_after_return`, `test_unrelated_tmp_survives_cleanup`. |
| A06 discarded pages | Streaming all pages; `test_collection_downloads_every_page` covers 1,001 members; benchmark covers 1,501 real synthetic artifacts. |
| A07 global ID suppression | Destination/provenance/requirements/quality/digest-backed reuse; `test_legacy_ids_are_preserved_but_not_trusted`, `test_second_playlist_destination_materializes`, `test_partial_album_retry_fetches_missing_members_only`. |
| A08 broken/reordered M3U | Build from ordered final artifacts, retain repeats; `test_m3u_root_album_disc_paths_resolve_and_repeat`, `test_concurrent_repeat_transfers_once_and_preserves_entries`. |
| A09 saved flags | Absent flags inherit settings; `test_saved_boolean_defaults_remain_effective`. |
| A10 CLI lifecycle | Maintenance without subcommand, lazy initialization, validated choices/ranges/patterns; `test_help_never_bootstraps_config`, `test_set_token_initializes_without_prompts`, `test_invalid_options_fail_before_side_effects`, installed-wheel smoke. |
| A11 config safety | Atomic/private serialized writes, literal tokens/redaction; `test_failed_config_write_preserves_original`, `test_concurrent_updates_preserve_independent_settings`, `test_config_is_private_and_token_is_literal`. |
| A12 initialization leak | Explicit close on initialization failure/cancellation; `test_failed_initialize_closes_client`. |
| A13 malformed responses | Validate auth/JSON/schema and source identity; `test_malformed_auth_is_typed_and_does_not_persist`, `test_metadata_identity_mismatch_does_not_publish`. |
| A14 retry classification | Three-attempt transient GET budget and bounded cancellable Retry-After; `test_get_retries_only_transient_statuses`, `test_retry_exhaustion_preserves_type_and_budget`, `test_cancel_during_retry_wait_prevents_more_requests`. Login POST is not replayed. |
| A15 encoded length | Compare encoded wire length coherently; `test_encoding_aware_transfer_integrity`, `test_truncated_unencoded_transfer_retries_and_cleans`. |
| A16 ranking | Filter eligibility before consistent ranking across pages; `test_smart_filter_ranks_eligible_candidates_only`. |
| A17 URL parsing | Exact host/path structures, malformed inputs isolated per request; `test_url_rejects_foreign_or_partial_paths`, `test_malformed_url_does_not_abort_other_requested_urls`. |
| A18 SQLite lifecycle | Explicit close and surfaced errors, additive backup-preserving schema; `test_connection_closed_without_gc`, `test_database_errors_are_not_silenced`, `test_history_failure_preserves_completed_artifact_outcome`. |
| A19 per-track quality | Every eligible track checks advertised and physical quality; `test_second_track_downgrade_respects_no_fallback`, `test_quality_upgrade_does_not_reuse_lower_quality_same_path`. |
| A20 throughput | Shared pool/transfer budget, bounded queues, repeated-request coalescing; `test_playlist_reaches_configured_global_concurrency`, `test_multiple_albums_do_not_multiply_transfer_budget`, `test_pending_work_is_bounded`, `test_cancel_stops_workers_and_closes_pool`. |
| A21 bundle parsing | Reject incomplete/empty secrets and malformed encoding; `test_incomplete_secrets_are_rejected`, `test_full_valid_bundle_extracts_secrets`. |
| A22 browser boundary | Exact intended response origin/path, success status, nonempty string token; `test_browser_accepts_only_successful_intended_response` uses fake Playwright. |

Additional coverage includes nullable metadata, safe destination containment,
signed-URL error redaction, corrupt-known-file recovery with retained originals,
same-quality embedding upgrades, stale artwork replacement, and failed settings
diagnostics. Automated tests isolate every CLI config/history path and reject
unmocked HTTP transport calls. No personal account, browser session, database, or
music download was used for qualification.

## Measurements

[Raw samples](2026-10-22/benchmark-results.json) contain five runs per scenario and
limit. Each run uses a fresh temporary destination, real generated MP3 data, real
Mutagen tagging, and real atomic publication. MockTransport supplies four delayed
chunks per stream. The large payload adds 512 KiB of valid ID3 padding. Slow-tag
runs add 5 ms per tagging call. Mixed failures return fatal 404s for two of twelve
members, assert no retry, preserve ten successful artifacts, and report failure.

| Twelve-track scenario | Limit 1 median | Limit 2 median | Limit 4 median |
| --- | ---: | ---: | ---: |
| Small payload | 272 ms | 189 ms | 163 ms |
| Large payload | 278 ms | 193 ms | 166 ms |
| Slow tagging | 352 ms | 210 ms | 165 ms |
| Mixed 404 | 226 ms | 162 ms | 139 ms |

All samples assert one pool, twelve requests, and active streams within the
selected limit. Small-payload limit 2 is approximately 31% faster than limit 1.
A separate full 1,501-artifact run at limit 4 took 20.99 seconds, transferred
304,332,253 bytes, peaked at four streams and 3.57 MB of traced Python memory,
and had a maximum heartbeat gap of 24.5 ms. A blocked-worker scheduler cancellation
probe settled in 0.087 ms. This is synthetic host evidence, not real CDN throughput,
HTTP connection-reuse measurement, RSS, or a cancellation-latency guarantee.
Regression tests separately cancel active streams and executor tagging.

Reproduce using the benchmark command in README. No timing threshold is enforced
in CI. Default limit 2 and 8 KiB output chunk size are retained.

## Qualification and remaining limits

- A user-authorized live attempt after the initial remediation proved token
  authentication and Studio membership succeed, but application-secret probing
  reports failure. Current external source evidence identifies the underlying
  service change: Qobuz retired `track/getFileUrl` around August 28, 2026 and the
  web player moved to `/session/start` plus `/file/url` with segmented encrypted
  CMAF. The existing direct-file transport cannot download from that protocol.
  This supersedes the earlier assumption that live CDN behavior was merely
  untested. The code now invalidates cached application credentials after browser
  login and maps legacy-endpoint HTTP 410 to a specific actionable error instead
  of claiming the valid login failed. Full replacement transport is not part of
  this remediation.

- Local regression suite: 132 passed on Python 3.13.7, 3.14.6, and 3.15.0 using
  cached runtime dependencies. Ruff and a cached mypy run targeting Python 3.13
  pass. Python 3.9.18 compiles all 38 Python files; this is syntax evidence only.
- Wheel and sdist build with cached Hatchling. Archive validation restricts source
  contents and rejects workspace logs/config/history/cache state. A fresh temporary
  wheel environment imports the installed package and exercises console help,
  synthetic token management, invalid options, real fake-network download/tagging,
  artifact reuse without another transfer, and nonzero failure exit.
- The fresh wheel environment reuses read-only installed runtime dependencies via
  a `.pth` fallback. It proves the project wheel imports and works, but is not a
  clean dependency installation. No globally installed tool was changed.
- `uv sync --frozen` cannot obtain packages because package-host DNS resolution
  fails. Locked dependency qualification and Python 3.9 runtime/type qualification
  remain outstanding. Python 3.9 syntax compilation is a separate local check.
  Added CI runs the lockfile on Python 3.9/3.14 across Linux/macOS/Windows; it has
  not run on the hosted service in this session.
- Real download/CDN behavior is blocked by the retired endpoint; browser token
  capture and Qobuz authentication were user-verified. FLAC metadata/nonempty data
  checks do not prove every compressed frame decodes. JPEG/PNG/PDF checks are bounded
  signature/end-marker checks, not full decoder validation.
- Hard-link atomic publication requires filesystem support. Same-host advisory
  locks do not certify remote filesystem lock semantics. Stable lock files are
  retained deliberately; do not delete them while operations may be running.

## Decisions and rollback

The Git metadata is read-only in this sandbox. Worktree/ref creation failed, so
changes remain in the current checkout without new commits. Recovery source bytes
and hashes are at `/private/tmp/qobuz-remediation-baseline-20261022`; the ignored
execution ledger and RED/GREEN logs are retained under
`.superpowers/sdd/2026-10-22-qobuz-remediation/` because Git history cannot replace
them. Do not reset this checkout: it includes pre-existing user edits.

Known audio updates retain `.previous-<uuid>` originals; additive DB migration
retains `.pre-artifacts-v1.bak`. To roll back a specific generated artifact, stop
all processes, preserve the current file separately, restore its matching original,
and allow history to be revalidated on the next run. Do not bulk-delete backups or
purge legacy rows as a repair shortcut.

Bounded queues apply per scheduling layer. Nested artist→album→track scheduling
uses O(limit²) pending metadata, rather than a single operation-wide `2*limit`
queue. This avoids queue deadlock and preserves independent album overlap; active
network streams remain globally bounded. Large caller-selected limits therefore
increase metadata/task overhead more quickly than linearly.

Artifact requirements include selected tag fields and completed artwork bytes as
well as naming/embed settings. Hash-only extras sidecars prevent unknown/stale art
from satisfying a different request. Atomic commits already started settle before
cancellation returns; they may leave a valid artifact to be revalidated next run.

## Final review

An independent fresh-context reviewer examined the complete changes and ran
additional offline probes. It found no Critical issues and three Important
issues that the initial 119 tests had missed. All three were reproduced with
failing regressions and fixed in one pass; the full suite then passed 132 tests:

1. FLAC artwork appended stale front covers, while MP3 could retain a front cover
   with a different APIC description. Both now replace type-3 pictures and preserve
   other picture types. `test_artwork_refresh_replaces_existing_front_cover` checks
   real FLAC/MP3 retagging, preserved back covers, no second audio transfer, and
   retained rollback files.
2. Missing/null/prematurely empty collection pages silently succeeded. Responses now
   validate block/items/count coherence before yielding, retain expected totals
   across pages, and fail when advertised members remain. Successful prior siblings
   survive in the failed aggregate. Genuine empty collections explicitly skip.
   Covered by `test_positive_collection_count_cannot_succeed_without_members`,
   `test_premature_empty_page_preserves_successful_siblings_and_fails`, and
   `test_true_empty_collection_is_explicit_skip`.
3. Album metadata advertising two tracks but containing only one could complete.
   Manifest completeness now validates before requesting media; malformed/truncated
   manifests fail and true zero-track albums skip. Covered by
   `test_truncated_album_manifest_cannot_claim_completion` and
   `test_true_zero_track_album_is_skipped`.

No Minor findings were reported. Changed totals/manifests fail coherently rather
than guessing an unsupported pagination endpoint. The reviewer declined live,
platform, clean-install, and decoder judgments already listed above; those remain
explicit qualification gaps. No second review was substituted for the verified
RED→GREEN fixes.

## Post-review live compatibility correction

The user subsequently demonstrated a successful browser token capture and Studio
authentication followed by `Can't find any valid app secret`. Root-cause tracing
found two layers: authentication succeeded, while initialization immediately
tested application secrets against Qobuz's retired direct-file endpoint. An
initial remediation also cleared `app_id`/`secrets` whenever browser login saved a
new user token, incorrectly coupling unrelated credentials and exposing the
stream failure again after a config rewrite. Follow-up regressions now preserve
application credentials, stop initialization after successful authentication,
defer secret selection until the first track-URL request, and state explicitly
that a stream rejection does not invalidate the login token. This restores login
and catalog access, but does not claim downloads work with Qobuz's replacement
encrypted segmented transport.
