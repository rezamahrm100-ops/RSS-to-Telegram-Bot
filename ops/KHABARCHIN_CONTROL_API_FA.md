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


## Native subscription bridge — v0.2 (source implemented, production NOT enabled)

The original RSStT command implementation already contains feed add/remove and
activation/deactivation logic. New optional `src/khabarchin_ipc.py` calls those
same functions from **inside the running bot process**, preserving its ORM,
scheduler updates, feed validation and subscription limits. It does not write
the SQLite database independently. `src/entrypoint.py` starts this adapter
only if the operator explicitly enables it.

**Container configuration (never commit live values):**
- `KHABARCHIN_IPC_ENABLED=1`
- `KHABARCHIN_MANAGED_CHANNEL_IDS=-1001234567890` (example only: replace
  with verified and explicitly approved numeric Telegram destination IDs)
- The existing `/app/config` volume must be mounted on the VPS host.
- The adapter binds **no TCP port**: its socket is `/app/config/.khabarchin-ipc.sock`,
  mode `0600`. It fails closed on a pre-existing socket rather than deleting
  a potentially live/foreign endpoint.
- API service: `--allow-subscription-write --allowed-channel -1001234567890`
  as separate opt-ins. Both API and bot independently enforce target IDs.
- The API/host process must have OS permission to connect to that socket.
  Do not weaken permissions or expose it publicly to make the connection work.

After an independently reviewed bot image deployment and a verified host-local
backup, the owner may opt in to a **single test destination**, first verifying
`/v1/capabilities` and a blocked cross-channel operation. Only then test
`POST /v1/commands` with one of these strict JSON payloads:

```json
{"action":"subscribe","channel_id":-1001234567890,"url":"https://example.org/feed.xml"}
```

```json
{"action":"deactivate","channel_id":-1001234567890,"subscription_id":123}
```

Other allowed actions are `unsubscribe` and `activate`; all require
`subscription_id` (integer) instead of `url`. The gateway uses the existing
VPS-local bearer, rejects unknown fields, audits only action/outcome/channel,
and forwards over a Unix socket after explicit enablement. Native rejections
return errors, not false success. Subscribe reuses RSStT's normal feed-fetch
behavior and may perform network requests; the input must be trusted.
The GitHub Issue command path refuses URL query parameters, credentials
and private/local IP destinations because Issue comments are not a secret vault.

**Not supported by this bridge:** arbitrary Telegram messages or media,
approval/rejection of queued articles, deleting all subscriptions, unrestricted
filesystem/SQL/SSH, generic command execution, OS installation, deployment,
or sending bot credentials to ChatGPT. Those features would require separate
reviewed adapters and opt-in permissions, not an unrestricted admin shell.

**Important production boundary:** The current VPS service was deployed as a
read-only API. Merely merging or updating this source branch does not enable
these write operations on VPS, nor does it prove that the updated bot image is
running. Rollback: disable the optional native adapter and API write flags
individually; preserve the config/database. Production tests must verify real
Telegram behavior and logs without uploading secrets or live DB to GitHub.

## REST API v0.3 — مدیریت تنظیمات اشتراک، بدون افزونه

این نسخه به API موجود اضافه می‌شود و **به‌صورت پیش‌فرض غیرفعال است**؛ انتشار کد در GitHub باعث نصب خودکار در VPS نمی‌شود.

### خواندن تنظیمات یک اشتراک

`GET /v1/subscriptions/{subscription_id}/settings?channel_id={channel_id}`

این مسیر تنها در صورت وجود شناسهٔ کانال در `--allowed-channel` و تعلق اشتراک به همان کانال، گزینه‌های تنظیمات را به‌شکل JSON برمی‌گرداند. اطلاعات محرمانهٔ تلگرام و نشانی فید در این خروجی وجود ندارد. API همچنان فقط روی `127.0.0.1` کار می‌کند.

### تنظیمات با عملیات نوع‌دار

`POST /v1/commands` با توکن محلی و `--allow-subscription-write`، فقط برای کانال مجاز:

```json
{"action":"set_interval","channel_id":-1001234567890,"subscription_id":123,"value":15}
```

```json
{"action":"set_title","channel_id":-1001234567890,"subscription_id":123,"value":"عنوان دلخواه"}
```

```json
{"action":"set_hashtags","channel_id":-1001234567890,"subscription_id":123,"value":"#علم #خبر"}
```

```json
{"action":"set_length_limit","channel_id":-1001234567890,"subscription_id":123,"value":1024}
```

```json
{"action":"set_option","channel_id":-1001234567890,"subscription_id":123,"field":"display_media","value":0}
```

مقادیر مجاز هر گزینهٔ شمارشی مطابق تنظیمات هستهٔ RSStT و مستندات OpenAPI است. `null` برای بازنشانی عنوان/هشتگ و فاصلهٔ بررسی مجاز است؛ مقدار `-100` در گزینه‌های شمارشی و محدودیت طول به‌معنای ارث‌بری تنظیم پیش‌فرض است. فاصلهٔ بررسی به دقیقه است و هستهٔ ربات همچنان حداقل مجاز خودش را اعمال می‌کند.

این API مستقیماً در پایگاه دادهٔ زنده از یک فرایند جداگانه نمی‌نویسد؛ فرمان از گذرگاه HTTP محلی به سوکت خصوصی در همان فرایند ربات می‌رسد. افزودن فید و تغییر فاصلهٔ بررسی از توابع آمادهٔ هسته استفاده می‌کنند؛ گزینه‌های شمارشی با اعتبارسنجی مطابق مقادیر اصلی و با ORM خود ربات ذخیره می‌شوند.

### مرزهای نسخهٔ فعلی

- هنوز واردکردن/خارج‌کردن OPML، صف تأیید/رد اخبار، اجرای تست انتشار و تنظیمات پیش‌فرض کاربران از API پوشش کامل ندارند.
- مسیر GitHub/WICC فعلاً فقط زیرمجموعه‌ای از عملیات را به ChatGPT ارائه می‌کند؛ پیاده‌سازی REST API به‌تنهایی مساوی قابلیت اجرای مستقیم تمام endpointها از این گفت‌وگو نیست.
- پیش از فعال‌سازی تولید: بکاپ **روی همان VPS**، تطبیق نسخهٔ دقیق کانتینر، آزمون رد کانال غیرمجاز، و آزمون واقعی روی کانال آزمایشی لازم است.


## گسترش API نسخهٔ ۰٫۴ — معادل تنظیمات پیش‌فرض و دکمه‌های گروهی

این نسخه **کد منبع است**؛ انتشار در GitHub به‌معنی ارتقای نسخهٔ فعال VPS یا فعال‌سازی نوشتن نیست. مسیر مورد تأیید مالک تنها ChatGPT ← GitHub ← رانر WICC موجود ← REST API محلی است. افزونهٔ جدید، MCP، رانر جدید یا پورت عمومی ممنوع است.

خواندن گزینه‌های پیش‌فرض یک مقصد مجاز:

```text
GET /v1/channels/-1001234567890/defaults
GET /v1/channels/-1001234567890/summary
```

نمونهٔ تنظیمات پیش‌فرض یک کانال (فقط از طریق API با توکن محلی و شناسهٔ مقصد مجاز):

```json
{"action":"set_default_interval","channel_id":-1001234567890,"value":15}
```

```json
{"action":"set_default_option","channel_id":-1001234567890,"field":"display_media","value":1}
```

```json
{"action":"set_default_length_limit","channel_id":-1001234567890,"value":1024}
```

**فعال/غیرفعال‌کردن همهٔ اشتراک‌های همان مقصد** فقط با دریافت تعداد اشتراک‌ها از مسیر `summary`، اعلام تعداد موردانتظار و تأیید صریح:

```json
{"action":"deactivate_all","channel_id":-1001234567890,"expected_count":3,"confirm":"DEACTIVATE_ALL"}
```

این فقط نمونه است و مجوز اعمال روی کانال واقعی محسوب نمی‌شود. فرمان `activate_all` نیز به `ACTIVATE_ALL` نیاز دارد. اختلاف تعداد اشتراک‌ها با مقدار تأییدشده موجب `409` می‌شود؛ عمل از توابع اصلی ربات انجام می‌شود.

بازنشانی تنظیمات یک اشتراک به حالت ارث‌بری (همان معنای دکمهٔ Reset در هسته):

```json
{"action":"reset_subscription","channel_id":-1001234567890,"subscription_id":123}
```

بازنشانی، عنوان دلخواه و هشتگ اشتراک را تغییر نمی‌دهد؛ فقط interval و گزینه‌های قالب‌بندی را بازنشانی می‌کند، مطابق کد Telegram. برای عملیات گروهی باید علاوه بر تعداد، مقصد دقیق و تأیید کاربر به‌شکل مستقل بررسی شود.

**محدودیت‌های باقی‌مانده:** فایل OPML، ارسال رسانه یا خبر آزمایشی، صف تأیید RSSPlus، تنظیمات مدیر و پشتیبانی کامل پنل Telegram هنوز پوشش جامع ندارند. اتصال IPC ربات در VPS همچنان یک گام استقرار مستقل و نیازمند بررسی کانتینر، پشتیبان و برنامهٔ بازگشت است.


## نسخهٔ ۰٫۵ — صف بررسی، رد و انتشار خبر با همان منطق دکمهٔ تلگرام

این نسخه عملیات RSSPlus را از دو مسیر، **دکمهٔ واقعی تلگرام و API**، به تابع مشترک `act_on_review` می‌فرستد؛ بدین ترتیب منطق انتشار و تغییر وضعیت در دو مسیر جداگانه تکرار نمی‌شود.

فهرست صرفاً اطلاعاتی موارد صف یک مقصد دارای مجوز:

```text
GET /v1/channels/-1001234567890/reviews
```

بدنهٔ خبر و فایل رسانه در این پاسخ وجود ندارد. شناسهٔ هر مورد، شناسهٔ اشتراک، زمان و وضعیت بازمی‌گردد. فایل RSSPlus در نسخهٔ فعلی VPS هنوز پیکربندی نشده و این عملیات تا زمان استقرار RSSPlus عملیاتی نخواهد بود.

فرمان‌های دقیق (برای کانال آزمایشی با شناسهٔ تأییدشده):

```json
{"action":"review_reject","channel_id":-1001234567890,"review_id":"abcdef0123456789","confirm":"REJECT_REVIEW"}
```

```json
{"action":"review_approve","channel_id":-1001234567890,"review_id":"abcdef0123456789","confirm":"PUBLISH_REVIEW"}
```

تأیید، **ارسال واقعی پیام/رسانه در تلگرام** انجام می‌دهد و برگشت‌پذیر نیست. به همین دلیل علاوه بر توکن، مجوز کانال، سوکت داخلی ربات و فلگ‌های نوشتن، تطبیق مورد در حال انتظار و عبارت تأیید صریح اجباری است.

انتقال شرطی SQLite از `pending` به `publishing` از انتشار دوبارهٔ هم‌زمان جلوگیری می‌کند. پس از ارسال موفق به `published` می‌رود؛ خطای ارسال قبل از تأیید، مورد را به `pending` بازمی‌گرداند. خرابی وب‌هوک اختیاری پس از ارسال، موجب گزارش ارسال مجدد نمی‌شود.

**مرز صداقت وضعیت:** فقط منطق کد و آزمون مصنوعی را می‌توان بدون راه‌اندازی RSSPlus اثبات کرد. موفقیت انتشار واقعی در کانال تست باید جداگانه مشاهده شود. به هیچ عنوان نباید با فعال‌سازی عجولانهٔ REST API یا ایجاد فید ساختگی در کانال اصلی آزمایش شود.
