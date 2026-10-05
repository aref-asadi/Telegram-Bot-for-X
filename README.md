# 🤖 Twitter/X → Telegram Timeline Bot

A production-ready, **multi-user** Telegram bot that streams each user's
personal X (Twitter) **Home Timeline** straight into their own Telegram chat —
with optional **one-click Persian translation via xAI Grok**.

Every credential (Twitter `auth_token`, `ct0` and the xAI API key) is collected
through an in-chat onboarding wizard and stored **per user** in a local SQLite
database. There are **no hard-coded user secrets** and **no `.env` credentials**.

---

## ✨ Features

| | |
|---|---|
| 👥 **Multi-user** | Unlimited users on one bot instance. Credentials, tweets and translation caches are fully isolated by `chat_id`. |
| ⚡ **1-click bookmarklet onboarding** | A drag-to-bookmarks bookmarklet reads `auth_token`/`ct0` from `document.cookie` on x.com, verifies them server-side (`POST /auth`) and returns the user to the chat via a short `t.me/…?start=auth_<code>` deep link — cookies never enter chat history. |
| 🧙 **Interactive onboarding** | A Persian-language `ConversationHandler` wizard guides each user: bookmarklet link first, a single-message manual cookie paste as fallback, then the optional Grok API key. |
| ✅ **Live verification** | Cookies are validated against X *before* the profile is saved — via the GraphQL `HomeTimeline` call, not the deprecated `account/settings.json` endpoint. Invalid cookies send the user back to step 1. |
| 🖼 **Complete media support** | Text, single photo, 2-10 photo albums, and videos/GIFs (highest-bitrate MP4). |
| ✂️ **Safe truncation** | Captions are truncated so the *visible* text always stays within Telegram's 1024-character caption limit. |
| 🌐 **AI translation (OpenAI-compatible)** | Each forwarded tweet carries a `[ 🌐 ترجمه با گروک ]` button. It prefers **that user's own** API key and falls back to the server-wide `AI_API_KEY`. `AI_BASE_URL`/`AI_MODEL` work with xAI Grok (default), OpenAI, OpenRouter or any self-hosted OpenAI-compatible server. Translations are cached in SQLite so nobody is ever billed twice. |
| 📰 **Dual feeds** | Two independent feeds: chronological **Following** (every tweet) and the algorithmic **For you** feed, quality-filtered by `FOR_YOU_MIN_LIKES` / `FOR_YOU_MIN_RETWEETS` / `FOR_YOU_MIN_IMPRESSIONS` and deduplicated against Following. Each user toggles For-you with `/feeds`. |
| ➖ **Not-interested feedback** | Every forwarded tweet has a `➖ علاقه‌مند نیستم` button: the message is removed from the chat and the feedback is relayed to X with the user's own session (best-effort, never raises back into Telegram). |
| 📱 **Mobile webview login** | A phone-only alternative to the bookmarklet: the wizard opens `<PUBLIC_BASE_URL>/webview?c=<id>`, a reverse proxy of x.com that rebinds cookies to the bot's origin, verifies them live and stores the session — no DevTools needed. |
| ⏸ **User controls** | `/status`, `/pause`, `/resume`, `/set_grok`, `/feeds`, `/logout`. |
| 🔄 **Auto-recovery** | If X invalidates a session (HTTP 401/403), that user is deactivated and notified to re-onboard. Other users are unaffected. |
| 🩺 **Free-hosting ready** | A tiny `aiohttp` server serves `GET /health` → `{"status":"ok"}` so Render / Koyeb / Railway keep the container alive. |
| 🐳 **Dockerised** | Slim Python 3.11 image, non-root user, persistent volume for SQLite. |

---

## 🏗 Architecture

```
                         ┌──────────────────────────────┐
                         │        main.py               │
                         │  (asyncio orchestration)     │
                         └───────────────┬──────────────┘
                                         │
        ┌────────────────────┬───────────┴───────────┬────────────────────┐
        │                    │                       │                    │
┌───────▼────────┐  ┌────────▼────────┐    ┌─────────▼─────────┐  ┌───────▼────────┐
│  web_server.py │  │   scheduler.py  │    │ Application (PTB) │  │  database.py   │
│ aiohttp /health│  │ per-user polling│    │ handlers+updates  │  │ aiosqlite      │
└────────────────┘  └────────┬────────┘    └─────────┬─────────┘  └───────┬────────┘
                             │                       │                    │
                   ┌─────────▼─────────┐   ┌─────────▼─────────┐          │
                   │ twitter_client.py │   │  bot_wizard.py    │          │
                   │ cookie auth + API │   │  bot_dispatcher.py│          │
                   └───────────────────┘   └─────────┬─────────┘          │
                                                     │                    │
                                            ┌────────▼────────┐           │
                                            │ grok_service.py │───────────┘
                                            │  xAI Grok API   │
                                            └─────────────────┘
```

**Design notes**

* **One process, four concurrent coroutines:** Telegram long-polling, the
  health-check web server, the background scheduler and all handler callbacks
  share a single `asyncio` event loop. It is very light — a free-tier instance
  is more than enough for dozens of users.
* **Per-user HTTP auth:** the X client reproduces the browser's request shape
  (public web bearer token + `auth_token`/`ct0` cookies + `x-csrf-token`
  header) and accepts the cookies *per call*, so requests from different users
  can never mix.
* **Tolerant parsing:** the GraphQL home-timeline payload is walked recursively
  for `tweet_results`, which keeps it working across X's frequent layout
  changes. If GraphQL yields nothing, the legacy v1.1 REST endpoint is used as
  a fallback.
* **Snowflake cursor:** tweets are compared numerically (`int(id) > last_id`),
  so deduplication never re-sends duplicates and never floods a chat.

---

## 📁 Project Structure

```
twitter-telegram-forwarder/
├── .env.example          # Template for infrastructure settings (never user creds)
├── .dockerignore
├── .gitignore
├── Dockerfile            # python:3.11-slim, non-root, healthcheck
├── docker-compose.yml    # persistent ./data volume for SQLite
├── requirements.txt
├── README.md
├── main.py               # Boots DB + bot + web server + scheduler
└── src/
    ├── __init__.py
    ├── config.py         # Env-backed Settings (lazy, cached)
    ├── database.py       # aiosqlite: users + translations_cache
    ├── twitter_client.py # Cookie-authenticated X client
    ├── grok_service.py   # Per-user Persian translation (xAI)
    ├── bot_wizard.py     # Onboarding ConversationHandler + commands
    ├── bot_dispatcher.py # Tweet formatting + media dispatch + translate button
    ├── scheduler.py      # Background polling loop
    ├── web_server.py     # aiohttp: /health + POST /auth + GET /bookmarklet
    ├── bookmarklet.js    # 1-click credential handover bookmarklet (source)
    └── bookmarklet.html  # installer page template served at /bookmarklet
```

---

## 🚀 Quick Start (local)

```bash
# 1. Clone
git clone https://github.com/aref-asadi/Telegram-Bot-for-X.git
cd Telegram-Bot-for-X

# 2. Create a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure *infrastructure only*
cp .env.example .env      # then edit .env and set BOT_TOKEN

# 5. Run
python main.py
```

Then open Telegram, find your bot and send **`/start`**.

> 💡 Create a bot and get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).

### ⚡ 1-click bookmarklet login

With `PUBLIC_BASE_URL` configured, `/start` offers a **bookmarklet installer
link** instead of the manual cookie guide:

1. The user drags the button from `https://<your-host>/bookmarklet?c=<chat_id>`
   to their bookmarks bar.
2. While logged in on **x.com**, they click it → the bookmarklet reads the
   cookies from `document.cookie` and `POST`s them to `/auth`, where they are
   verified live against X.
3. The server stashes the session under a **12-character one-time code**
   (Telegram deep links only allow 64 characters, so the cookies themselves
   never travel through Telegram) and the browser redirects to
   `t.me/<bot>?start=auth_<code>`.
4. The wizard redeems the code, saves the user to SQLite and kicks an
   immediate timeline poll — done in one click.

**No `PUBLIC_BASE_URL`?** The wizard falls back to a manual guide where both
cookies are pasted in a **single message**. The bookmarklet also copies that
paste-ready payload to the clipboard whenever the server is unreachable, so
the manual path always works.

---

## 🔧 Environment Variables

Only **operator / infrastructure** settings live in `.env`. User credentials are
**never** read from the environment.

| Variable | Default | Required | Description |
|---|---|---|---|
| `BOT_TOKEN` | – | ✅ | Telegram bot token from **@BotFather**. |
| `PORT` | `8000` | – | Port for the health-check web server. Render injects `10000`. |
| `PUBLIC_BASE_URL` | – | – | Public `https://` origin (e.g. `https://my-bot.onrender.com`) used to build the bookmarklet page and its `POST /auth` endpoint. Render's `RENDER_EXTERNAL_URL` is picked up automatically; with no base URL the wizard falls back to manual cookie paste. |
| `DATABASE_PATH` | `data/bot.db` | – | SQLite file path. The parent folder is created automatically. |
| `POLL_INTERVAL_SECONDS` | `180` | – | How often each active user's timeline is polled (minimum 30). |
| `MAX_TWEETS_PER_POLL` | `20` | – | Timeline window size fetched per poll, per user. |
| `XAI_MODEL` | `grok-3` | – | Grok model used for translation. Users supply their own key. |
| `LOG_LEVEL` | `INFO` | – | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`. |

---

## 🤖 Bot Commands

| Command | What it does |
|---|---|
| `/start` | Greets the user and starts the wizard; `/start auth_<code>` completes the bookmarklet handover. |
| `/status` | Shows whether forwarding is active and whether Grok is linked. |
| `/pause` | Stops forwarding tweets to this chat. |
| `/resume` | Resumes forwarding. |
| `/set_grok` | Adds or replaces the personal xAI API key. |
| `/logout` | Deletes the user's cookies, key and cached translations. |
| `/help` | Shows the command reference. |
| `/cancel` | Aborts the current wizard step. |

### The translate button

Every forwarded tweet carries an inline button:

```
[ 🌐 ترجمه با گروک ]   ->   callback_data = "tr_<tweet_id>"
```

* **No key set?** The user sees an alert telling them to run `/set_grok`.
* **Key set?** The tweet is translated into fluent Persian and posted as a
  **reply** to the original message.
* **Already translated?** The cached translation is served instantly from
  SQLite — the user is never charged twice.


---

## ☁️ Deployment

### Option A — Render (Free Web Service)

Render's free tier needs an HTTP port to health-check; this bot provides it.

1. Push this repository to GitHub.
2. On <https://dashboard.render.com> → **New → Web Service** → connect the repo.
3. Configure:
   * **Environment:** `Docker` (Render builds the provided `Dockerfile`), **or**
     `Python 3` with
     * **Build Command:** `pip install -r requirements.txt`
     * **Start Command:** `python main.py`
   * **Instance type:** `Free`
4. Add environment variables (**Environment → Add Environment Variable**):
   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | your token from @BotFather |
   | `DATABASE_PATH` | `/opt/render/project/src/data/bot.db` |
5. *(Recommended)* Add a **Render Disk** mounted at `/opt/render/project/src/data`
   so the SQLite database survives redeploys. Without a disk, every deploy wipes
   all users and they must run `/start` again.
6. Deploy. Render will call `GET /health` and expect `{"status":"ok"}`.

> ℹ️ Render sets `PORT` (usually `10000`) automatically — don't hard-code it.

> ⚠️ Free instances sleep after ~15 minutes of inactivity. The bot will resume
> automatically on the next incoming update; users may simply need to wait a few
> seconds for the first message after a sleep.

### Option B — Koyeb

1. Push the repo to GitHub.
2. On <https://app.koyeb.com> → **Create Service → GitHub** → pick the repo.
3. **Builder:** *Dockerfile* (detected automatically).
4. **Ports:** add a port with **Port** `8000`, protocol `HTTP`, exposed as `8000`.
5. **Health checks:** HTTP, path `/health`, port `8000`.
6. **Environment variables:** `BOT_TOKEN`, and `DATABASE_PATH=/app/data/bot.db`.
7. **Volumes:** add a volume mounted at `/app/data` (size: the free minimum is
   fine) so the SQLite file persists.

Koyeb injects `PORT`; the app reads it automatically.

### Option C — Railway

1. **New Project → Deploy from GitHub repo.**
2. Railway auto-detects the `Dockerfile`.
3. **Variables:** `BOT_TOKEN` (Railway sets `PORT` itself).
4. **Volumes:** attach a volume at `/app/data` and set
   `DATABASE_PATH=/app/data/bot.db`.

### Option D — Docker / docker-compose (VPS, home server, NAS)

```bash
cp .env.example .env      # set BOT_TOKEN
docker compose up -d --build
docker compose logs -f
```

The compose file mounts `./data:/app/data`, so `data/bot.db` lives on the host
and survives every rebuild.

Run without compose:

```bash
docker build -t telegram-bot-for-x .
docker run -d --name tg-x \
  --env-file .env \
  -e DATABASE_PATH=/app/data/bot.db \
  -p 8000:8000 \
  -v "$PWD/data:/app/data" \
  --restart unless-stopped \
  telegram-bot-for-x
```

### Verifying deployment

```bash
curl http://<your-host>:<port>/health
# -> {"status": "ok"}
```

### Persistence checklist (important!)

The SQLite file contains every user's cookies. **Always mount a persistent
volume** at the directory you point `DATABASE_PATH` to:

* Render → a Render **Disk**
* Koyeb → a Koyeb **Volume**
* Railway → a Railway **Volume**
* Docker → a bind mount / named volume


---

## 🧑‍💻 User Guide

Each user completes these steps **once**, inside their private chat with the bot.

### Step 1 — Get your Twitter `auth_token`

1. Open a browser and log in to **<https://x.com>**.
2. Press **F12** (or `Ctrl+Shift+I` / `Cmd+Option+I`) to open **Developer Tools**.
3. Open the **Application** tab.
   * *Firefox:* use the **Storage** tab instead.
4. In the left sidebar expand:
   **Storage → Cookies → `https://x.com`**
5. Find the cookie named **`auth_token`** and copy its **Value**.
   * It is a long string of letters and numbers (~40 characters).
6. Paste it into the chat with your bot.

### Step 2 — Get your `ct0`

In that **same** cookie table:

1. Find the cookie named **`ct0`**.
2. Copy its **Value** (a long hexadecimal string).
3. Paste it into the chat with your bot.

The bot then performs a **live check** against X:

* ✅ Success → you continue to step 3.
* ❌ Failure → *"کوکی‌ها نامعتبرند یا منقضی شده‌اند"* and you re-enter step 1.

> 🔐 Tip: the bot tries to delete the messages containing your cookies as soon
> as it reads them.

### Step 3 — (Optional) Connect Grok for Persian translations

1. Go to **<https://console.x.ai>** and sign in / sign up.
2. Open **API Keys** and click **Create API Key**.
3. Copy the key — it starts with **`xai-`**.
4. Paste it into the chat.

Not interested right now? Tap
**`⏭️ رد کردن / فعلاً بدون هوش مصنوعی`** (or send `/skip`). You can add a key
later at any time with **`/set_grok`**. If the operator configured a server-wide
`AI_API_KEY`, the translate button also works without a personal key — your own
key just keeps billing fully isolated.

Once finished, the bot replies with a success message and begins polling. The
first successful poll seeds the timeline pointer and sends a short
*"پایش تایم‌لاین شما آغاز شد"* confirmation, after which only **new** tweets are
forwarded.

---

## 🔄 How It Works (internals)

1. **Auth** — The client sends the browser's public web bearer token plus the
   user's `auth_token` and `ct0` cookies, with `ct0` mirrored into the
   `x-csrf-token` header (the double-submit CSRF pattern X's own web app uses).
2. **Fetch (two feeds)** — Each poll reads the chronological **Following** feed
   (`HomeLatestTimeline`) and — unless disabled via `/feeds` — the algorithmic
   **For you** home feed. If GraphQL returns nothing (query-id drift / soft
   failure), the legacy v1.1 `statuses/home_timeline.json` endpoint is used as a
   fallback.
3. **Parse** — The payload is walked recursively for `tweet_results`, unwrapping
   visibility wrappers and retweets, extracting author, body (including
   long-form `note_tweet` text), photos, and the highest-bitrate MP4 variant for
   videos/GIFs.
4. **Deduplicate & filter** — Tweet IDs are snowflakes: any tweet whose numeric
   id is greater than the stored `last_tweet_id` (`last_for_you_id` for the
   For-you feed) is new. New tweets are sent **oldest first** so the chat reads
   chronologically. For-you tweets are additionally deduplicated against the
   Following pointer and dropped when they miss the `FOR_YOU_MIN_*` thresholds.
5. **Deliver** — Media is dispatched with the correct Telegram method (album →
   `send_media_group`, photo → `send_photo`, video → `send_video`, text →
   `send_message`), each with a truncated caption and the translate button.
6. **Translate** — Pressing the button loads *that user's* key, checks the
   SQLite translation cache, and only calls xAI on a cache miss.

---

## 🔐 Security & Privacy

* **Everything is stored locally.** Credentials live in a SQLite database on
  *your* host. The bot sends cookies to exactly two places: **x.com** (to read
  your timeline) and, only if you press the translate button, your own xAI key
  to **api.x.ai**. Nothing is forwarded to any third party.
* **Strictly per-user isolation.** Cookies, Grok keys, last-seen tweet ids and
  translation caches are all keyed by `chat_id`. One user can never read or
  trigger another user's data.
* **No credentials in `.env`.** `BOT_TOKEN` is the only secret in the
  environment; it belongs to the *operator*, not to users.
* **Never commit the database.** `.gitignore` and `.dockerignore` exclude
  `data/`, `*.db` and `.env`, so the credential file cannot leak into Git or a
  container image.
* **User consent.** Users copy cookies out of their own authenticated session
  and may revoke everything at any time with `/logout`, which deletes their row
  and their cached translations.
* **Run it only on accounts you own / are authorised to use.** Session cookies
  are powerful — treat them like passwords.

> ⚠️ **Disclaimer:** This project uses X's internal, undocumented endpoints by
> replaying the same requests the web app makes. It is not affiliated with or
> endorsed by X Corp. or xAI. These endpoints can change without notice, and
> automated access may conflict with X's Terms of Service. Use at your own risk,
> preferably with a dedicated/burner account.

