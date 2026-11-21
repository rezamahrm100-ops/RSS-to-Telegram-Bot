# Khabarchin Control API v0.1 — owner-only VPS service

## Architecture
ChatGPT → authenticated GitHub connector → **private** owner Actions control plane → existing authorized **self-hosted** runner → pinned SSH host → **localhost-only** API on the same VPS → RSStT/RSSPlus data.

The public RSStT fork stores **source code only**. It must **never** run untrusted pull-request code or owner SSH secrets on a self-hosted runner. Do not use `pull_request_target` to check out untrusted branch code.

This API is separate from the Telegram bot process. It reads upstream SQLite in **mode=ro** and RSSPlus's existing `rssplus.json`; it does not duplicate feed polling, Telegram publishing, or the subscription database. It uses Python's standard library and runs as a single opt-in host-local service.

## Implemented in v0.1

| Method | Route | Capability |
|---|---|---|
| GET | `/v1/health` | Gateway health; explicitly does **not** claim the Telegram bot is alive |
| GET | `/v1/capabilities` | Exact current allowlist |
| GET | `/v1/status` | RSS/Subscription counts from the existing SQLite schema |
| GET | `/v1/feeds?limit=20` | Read-only feed metadata (1–50) |
| GET | `/v1/subscriptions?limit=20` | Read-only subscription metadata (1–50) |
| GET | `/v1/reviews?limit=20` | RSSPlus queue metadata; **not** saved article bodies |
| GET | `/v1/rules` | Current filter policies; redacts webhook token and other top-level secrets |
| PUT | `/v1/rules/{channel_id}` | Optional, strictly typed partial rule update with `If-Match` ETag |

The write route is **disabled by default**. No arbitrary SQL, filesystem, command execution, deployment or Telegram send endpoints exist. Adding them requires reviewed adapters with operation-specific authorization and production verification. The external Replit editor is **not** magically accessible through this API; repository edits, tests and releases remain a separate GitHub workflow.

## VPS-local security and configuration

1. Verify the **actual** Docker mount and `DATABASE_URL` first. **Never guess** which SQLite file is primary.
2. Use a dedicated least-privileged local account with read access to the RSStT SQLite and RSSPlus config; grant config write only when rule editing is explicitly approved.
3. Generate a unique random secret **on the VPS**, store it in a local `0600` file outside the repository (e.g. `/etc/khabarchin/control.token`), never in GitHub secrets/artifacts/logs or this README.
4. Start the API **only on 127.0.0.1:8765**. Do not publish its port in Docker, Caddy, UFW or public DNS; use an already-authorized SSH route from the owner runner.
5. Example (replace **paths based on a verified host-local inventory**, not assumptions):

```bash
python3 ops/khabarchin_control_api.py \
  --config-dir /srv/EXACT_RSSTT_CONFIG \
  --database /srv/EXACT_RSSTT_CONFIG/EXACT_MAIN_DB.sqlite3 \
  --token-file /etc/khabarchin/control.token \
  --port 8765
```

To enable rule updates **after authorization and testing**, add `--allow-rules-write`. The caller first GETs `/v1/rules`, obtains its `ETag` and sends that value in `If-Match` on PUT, alongside a JSON policy such as `{"mode":"review","include":["science"],"exclude":["advertisement"]}`. Updates atomically replace the existing RSSPlus config without removing webhook settings. Minimal event + outcome audit records are stored locally at `.khabarchin-api-audit.jsonl`, with no token or request body.

## RSStT-only mode (RSSPlus not configured)

If the bot's actual persistent `/app/config` directory has **no** `rssplus.json` and no `rssplus.sqlite3`, that is a valid upstream RSStT-only installation. On API v0.1, `GET /v1/status` still reads the verified upstream SQLite and reports `rssplus_configured: false` and `rssplus_enabled: false`. `GET /v1/rules` reports empty disabled policies without an ETag; `GET /v1/reviews` reports an empty unavailable queue. Rule updates NEVER create an RSSPlus config; they fail closed.

Do not synthesize or enable RSSPlus merely to run the base status API. Any malformed, oversized or symlinked *present* RSSPlus config still fails closed. The owner-only WICC preflight must first establish the exact active SQLite default path and confirm no conflicting DB/env override.

## Backups: local VPS only

**Owner decision:** backups **must stay on the same VPS** at `/var/backups/khabarchin/rsstt`; **no external/cloud/GitHub backup**. The existing backup utility in PR #3 must be reviewed and applied separately. This API neither runs backups nor uploads them. Total VPS/disk failure remains an accepted risk.

## Tests

```bash
python3 -m unittest discover -s tests -p 'test_khabarchin_control_api.py' -v
```

Sixteen synthetic offline tests cover authentication, host allowlisting, database visibility, secret redaction, immutable review payloads, default read-only configuration, ETag conflict checks, validation, audit, malformed limits and missing DBs. Sixteen synthetic tests are defined; the current source revision has no GitHub Actions PASS evidence, and local fixture PASS is **not** VPS deployment proof.

## Release gates

- Merge only after code review on exact SHA; no secrets or environment values committed.
- Verify bound port is loopback-only and cannot be reached publicly.
- Confirm the real SQLite location and bot process independently; do not conflate gateway health with bot health.
- Test API calls through the existing private GitHub Actions owner runner with a pinned SSH identity.
- Verify native Telegram subscription and publishing flow separately.
- Rollback by stopping only the API service; RSStT itself must remain unaffected.

## وضعیت آزمون جاری

آزمون‌های چهاردهم تا شانزدهم شامل بررسی جلوگیری از عبور توکن و نبود تنظیمات RSSPlus هستند. آزمون اولیه برای جلوگیری از عبور توکن از پراکسی‌های محیطی اضافه شد. گردش‌کار GitHub-hosted فقط داده ساختگی اجرا می‌کند؛ هیچ اجرای تولیدی یا ارسال داده واقعی انجام نمی‌شود. برچسب `PASS` قبلی فقط به نسخه پیشین کد مربوط است، نه آخرین Commit.
