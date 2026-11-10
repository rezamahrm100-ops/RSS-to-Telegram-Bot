# RSSPlus — VPS deployment of RSS-to-Telegram-Bot

A minimal AGPL-3.0-or-later extension of [RSS-to-Telegram-Bot](https://github.com/Rongronggg9/RSS-to-Telegram-Bot).
Uses the original feed parsing, media handling, subscriptions and Telegram publishing.
The additions are per-channel keyword filters, URL deduplication, Telegram moderation
buttons, and an optional metadata webhook for external automations.

## VPS quickstart (Ubuntu/Debian Linux with Docker Engine, Compose plugin, git, and python3)

After merging the feature pull request:

~~~bash
git clone https://github.com/rezamahrm100-ops/RSS-to-Telegram-Bot.git
cd RSS-to-Telegram-Bot
bash rssplus-install.sh
~~~

Enter BotFather TOKEN and your numeric Telegram MANAGER user ID when prompted.
The installer creates local, gitignored credentials, builds from **your modified
source**, and launches with Docker Compose. Do not enter secrets into GitHub.

Add the bot to a Telegram channel with permission to post. Use its original
/sub and /set commands to subscribe a channel to RSS feeds. Images, video,
audio and attached documents are sent only if available in the feed or fetchable
by the original parser. No public VPS ports need be opened for the bot.

## Configure filters and moderation

Edit the local file config/rssplus.json. Example:

~~~json
{
  "enabled": true,
  "review_chat_id": 123456789,
  "default": {
    "mode": "auto",
    "deduplicate": true,
    "include": [],
    "exclude": []
  },
  "targets": {
    "-1001234567890": {
      "mode": "review",
      "include": ["science", "پژوهش"],
      "exclude": ["sponsored", "تبلیغ"]
    }
  },
  "webhook_url": "",
  "webhook_token": ""
}
~~~

Target keys are numeric Telegram chat/channel IDs. If a channel is set to
"review", previews go to review_chat_id (your private manager account or a
designated private moderator chat) with Approve/Reject buttons. Only MANAGER
accounts can approve. Use /rssplus_pending in your private bot chat to
resurface the last 20 queued items. "auto" publishes without manual review.

"include" matches at least one keyword if not empty. "exclude" rejects any
matching keyword. These are literal, case-insensitive substrings, not AI
semantic filters. Deduplication is per destination channel, based on a
normalized article URL; posts without links use title + text. It does not
detect different articles about the same event.

Changes to config/rssplus.json apply to future items without restart.
Existing records live in config/rssplus.sqlite3; never delete it casually.
Keep the whole config/ directory backed up.

## Optional external automation

An HTTPS webhook_url receives JSON metadata after a publishing attempt:
event, status, channel_id, title, link, feed. webhook_token, if provided, is sent
as Bearer authorization. This **does not** upload media or publish to Instagram,
X, etc.; a separate workflow and appropriate API access are required. Webhook
failures are logged but are not retried automatically.

## Operational limits and safety

The upstream notifier may swallow some Telegram send failures. "published" in
RSSPlus means the send method finished; it is not guaranteed delivery. Preview
messages are title/link only, but approving reparses the archived feed entry and
uses the original rich-media publishing path. Old media URLs can expire.
Telegram flood limits and source access rules apply. Review the logs after
testing in a private channel:

~~~bash
docker compose logs -f --tail=100
~~~

The fork is public so users of the modified bot can access the corresponding
source under the upstream AGPL license. Do not commit .env, API credentials,
Telegram session files or runtime database contents.
