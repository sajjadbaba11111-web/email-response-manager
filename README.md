# Email ↔ WhatsApp AI Agent

A personal assistant that connects your **Gmail** account to your **WhatsApp**
number through **Claude**.

* Every morning it summarises the last 24 hours of mail and pushes the summary
  to your WhatsApp.
* Whenever a new email arrives it drafts a reply and sends you the draft with
  an **id**.
* You approve with a single message on WhatsApp:
  * `send 12` &nbsp;→ sends the draft exactly as written
  * `send 12 thanks, Thursday works` &nbsp;→ sends **your** wording instead
  * `discard 12` &nbsp;→ throws the draft away
* Every capability is also exposed as an **MCP** server served by the same
  FastAPI application at `/mcp`, so Claude Desktop (or any other MCP client)
  can use it.

> **Nothing is ever emailed without your explicit `send` command.**
> The agent can read, summarise and *draft*, but sending is always your call.

---

## Table of contents

1. [Architecture](#architecture)
2. [Requirements](#requirements)
3. [Setup](#setup)
   * [3.1 Google Cloud / Gmail OAuth](#31-google-cloud--gmail-oauth)
   * [3.2 Meta WhatsApp Cloud API](#32-meta-whatsapp-cloud-api)
   * [3.3 Local webhook with ngrok](#33-local-webhook-with-ngrok)
   * [3.4 Anthropic](#34-anthropic)
4. [Configuration](#configuration)
5. [Running it](#running-it)
6. [WhatsApp commands](#whatsapp-commands)
7. [Admin dashboard (email-response activity)](#admin-dashboard-email-response-activity)
8. [MCP endpoint / Claude Desktop](#mcp-endpoint--claude-desktop)
9. [The 24 hour WhatsApp window](#the-24-hour-whatsapp-window)
10. [Tests and linting](#tests-and-linting)
11. [Docker & PostgreSQL](#docker--postgresql)
12. [Security notes](#security-notes)
13. [Troubleshooting](#troubleshooting)
14. [Design decisions and assumptions](#design-decisions-and-assumptions)

---

## Architecture

```
                 ┌──────────────────────── FastAPI (app/main.py) ─────────────────────────┐
                 │                                                                          │
   Meta  ─POST──► │  /webhook/whatsapp        /mcp (MCPServer, bearer token)     /health     │
                 │        │                              │                           │      │
                 │        ▼                              ▼                           │      │
                 │  routes_whatsapp.py           mcp_server.py                      │      │
                 │   • HMAC signature check       • list_emails                    │      │
                 │   • sender allow-list          • read_email                     │      │
                 │   • message-id dedup           • draft_reply                    │      │
                 │        │                       • send_email_reply               │      │
                 │        ▼                       • send_whatsapp                  │      │
                 │  services.py ◄───────────────────────┘                           │      │
                 │   • check_new_mail  • daily_summary  • handle_command              │      │
                 │        │              │                 │                          │      │
                 │        ▼              ▼                 ▼                          │      │
                 │  gmail_client.py  llm.py         whatsapp.py                      │      │
                 │   (asyncio.to_thread) (Claude)    (Meta Cloud API)                │      │
                 │        │                                                                   │
                 │        ▼                                                                   │
                 │  db.py (PostgreSQL primary / SQLite fallback: drafts, seen_emails,          │
                 │         processed_wa_messages, email_snapshots, activity_log)              │
                 │                                                                           │
                 │  scheduler.py (APScheduler: poll every N min, cron summary at HH:MM)     │
                 └───────────────────────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
| --- | --- |
| `app/main.py` | FastAPI app, lifespan, MCP mount, ASGI bearer-token middleware, `/health`, admin mount |
| `app/config.py` | `pydantic-settings` configuration, fail-fast validation |
| `app/prompts.py` | **placeholder** – replace with your own prompts (same interface) |
| `app/db.py` | Dual-backend persistence: **PostgreSQL** when `DATABASE_URL` is set, **SQLite** fallback; drafts, seen emails, WA dedupe, app state, **email snapshots**, **activity log** |
| `app/routes_admin.py` | Credential-protected admin dashboard (login, activity, drafts/responses, email snapshots) |
| `app/templates/admin/*` | Server-rendered Jinja2 admin UI |
| `app/gmail_client.py` | Gmail list/fetch/send, body extraction, thread-aware replies |
| `app/whatsapp.py` | `WhatsAppClient` protocol + Meta Cloud API implementation |
| `app/llm.py` | Async Claude wrapper: `draft_reply()`, `summarize()`, retries |
| `app/services.py` | Business logic: `check_new_mail`, `daily_summary`, `handle_command`; snapshots emails and logs response/activity events |
| `app/scheduler.py` | APScheduler job registration |
| `app/mcp_server.py` | `MCPServer` tools (the `mcp` 2.x API) |
| `app/routes_whatsapp.py` | Webhook verification + inbound message handling |
| `app/logging_config.py` | JSON logging with secret redaction |
| `scripts/gmail_auth.py` | One-time OAuth flow writing `token.json` |

---

## Requirements

* Python **3.11+** (developed and tested on 3.11–3.14)
* A Google account with access to the mailbox you want to bridge
* A Meta **WhatsApp Business** account with the Cloud API enabled
* An Anthropic API key

---

## Setup

### 3.1 Google Cloud / Gmail OAuth

1. Open the [Google Cloud console](https://console.cloud.google.com/) and
   create (or pick) a project.
2. Enable the **Gmail API** (`APIs & Services → Library`).
3. Configure the OAuth consent screen:
   * `APIs & Services → OAuth consent screen`
   * choose *External* (or *Internal* if you have a Workspace account)
   * add your own Google account as a **test user**
   * the app stays in *testing* mode, which is what we want — no verification
     review needed for a personal project.
4. Create the credentials:
   * `APIs & Services → Credentials → Create Credentials → OAuth client ID`
   * application type: **Desktop app**
   * download the JSON and save it as `client_secrets.json` in the project root.
5. Run the one-time authorisation:

   ```bash
   python scripts/gmail_auth.py
   ```

   A browser opens, you log in with **your own** account and grant access.
   The script writes `token.json` (mode `0600`). The token is refreshed
   automatically by the application and written back to disk.

The requested scopes are:

| Scope | Why |
| --- | --- |
| `https://www.googleapis.com/auth/gmail.readonly` | list and read emails |
| `https://www.googleapis.com/auth/gmail.send` | send the approved replies |

> **Security note.** `gmail.send` allows the application to send mail as you.
> The human-in-the-loop design in this project is what keeps that safe: the
> agent never calls it without an explicit `send` command from your WhatsApp
> number.

### 3.2 Meta WhatsApp Cloud API

1. Create a **Meta developer account** and a Business app
   (<https://developers.facebook.com/apps>).
   - App type: **Business**
   - Use case: **Other** (not "Connect with customers")
2. Add the **WhatsApp** product and set up the **Cloud API**.
3. In *WhatsApp → API Setup* you get:
   * a **temporary access token** (24 h) for testing — for production create a
     **System User** token with the `whatsapp_business_messaging` permission,
   * your **Phone number id**,
   * the **WhatsApp Business Account id**.
4. Copy your **App Secret** from *App Settings → Basic* → this is
   `WHATSAPP_APP_SECRET` and it is used to verify webhook signatures.
5. In *WhatsApp → Configuration → Webhooks*:
   * **Callback URL**: `https://<your-host>/webhook/whatsapp`
   * **Verify token**: any random string → put it in `WHATSAPP_VERIFY_TOKEN`
   * subscribe to the `messages` field.

### 3.3 Local webhook with ngrok

Meta cannot reach `localhost`, so expose your local server with a tunnel:

```bash
ngrok http 8000
```

ngrok prints a public URL, e.g. `https://1a2b-3c4d.ngrok-free.app`. Use
`https://1a2b-3c4d.ngrok-free.app/webhook/whatsapp` as the Meta callback URL.

Keep the ngrok URL stable (the free tier changes it on restart) or use a
paid/static domain, otherwise you have to update the Meta webhook each time.

### 3.4 Anthropic

Create an API key at <https://console.anthropic.com/settings/keys> and put it
in `ANTHROPIC_API_KEY`. The model id is configurable via `CLAUDE_MODEL`
(default `claude-sonnet-5-5`).

---

## Configuration

Copy the template and fill it in:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `ANTHROPIC_API_KEY` | ✅ | – | Anthropic API key |
| `CLAUDE_MODEL` | | `claude-sonnet-5-5` | Model id used for drafting and summarising |
| `WHATSAPP_TOKEN` | ✅ | – | Meta Cloud API access token |
| `WHATSAPP_PHONE_ID` | ✅ | – | Sender phone-number id |
| `WHATSAPP_VERIFY_TOKEN` | ✅ | – | Token echoed during webhook verification |
| `WHATSAPP_APP_SECRET` | ✅ | – | Meta app secret, used for the HMAC signature check |
| `MY_WHATSAPP_NUMBER` | ✅ | – | Your WhatsApp number, digits only (`923001234567`) |
| `WHATSAPP_TEMPLATE_NAME` | | `daily_summary_opener` | Approved template for the 24 h window fallback |
| `WHATSAPP_TEMPLATE_LANGUAGE` | | `en` | Template language code |
| `GMAIL_TOKEN_PATH` | | `token.json` | OAuth token file produced by `scripts/gmail_auth.py` |
| `SUMMARY_HOUR` | | `8` | Hour (0–23) of the daily summary |
| `SUMMARY_MINUTE` | | `0` | Minute of the daily summary |
| `SUMMARY_TIMEZONE` | | `Asia/Karachi` | IANA timezone of the summary job |
| `POLL_INTERVAL_MINUTES` | | `2` | Inbox polling interval |
| `DATABASE_URL` | | *(empty)* | PostgreSQL URL (e.g. `postgresql://user:pass@host:5432/db`). **Primary when set**; SQLite is used when empty. Docker Compose app service defaults to the compose Postgres service. |
| `DB_PATH` | | `data/agent.db` | SQLite database file (fallback when `DATABASE_URL` is empty) |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_PORT` | | see `.env.example` | Docker Compose PostgreSQL container settings |
| `ADMIN_USERNAME` | | `admin` | Admin dashboard username |
| `ADMIN_PASSWORD` | | *(empty)* | Admin dashboard password. Admin UI is enabled only when set together with `ADMIN_SECRET_KEY`. |
| `ADMIN_SECRET_KEY` | | *(empty)* | Secret for signing admin session cookies (use a long random value). |
| `MCP_API_KEY` | ✅ | – | Bearer token protecting `/mcp` |
| `LOG_LEVEL` | | `INFO` | Root log level |
| `LLM_MAX_TOKENS` | | `1024` | Max output tokens per Claude call |
| `LLM_TIMEOUT_SECONDS` | | `60` | Claude request timeout |
| `LLM_MAX_RETRIES` | | `3` | Retries with exponential backoff |
| `MAX_EMAIL_BODY_CHARS` | | `6000` | Email body truncation limit |
| `ERROR_NOTICE_COOLDOWN_MINUTES` | | `30` | Minimum gap between two identical error notices |
| `GMAIL_MAX_RESULTS` | | `50` | Maximum emails fetched per query |

Missing required variables abort the startup with a clear message:

```
Missing required environment variable(s): ANTHROPIC_API_KEY, MCP_API_KEY.
Copy .env.example to .env and fill in the values.
```

---

## Running it

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt      # runtime *and* dev dependencies

cp .env.example .env                 # then edit it
python scripts/gmail_auth.py         # one-time browser authorisation

uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

`requirements.txt` is fully pinned and contains **both** the runtime and the
development dependencies (pytest, pytest-asyncio, respx, ruff), so a single
install is enough to run the app *and* the test suite.

Endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/health` | DB, Gmail token and scheduler status |
| `GET` | `/webhook/whatsapp` | Meta verification handshake |
| `POST` | `/webhook/whatsapp` | Inbound WhatsApp messages |
| `GET/POST` | `/admin/*` | Admin dashboard (credential-protected; enabled with `ADMIN_PASSWORD` + `ADMIN_SECRET_KEY`) |
| `*` | `/mcp` | MCP server (bearer token required) |
| `GET` | `/docs` | OpenAPI documentation |

The **first startup** seeds `seen_emails` with everything that is already in
your inbox, so the agent does not flood you with drafts for old mail. The
seeding only happens once (a flag is stored in the `app_state` table).

---

## WhatsApp commands

| Command | Effect |
| --- | --- |
| `send 12` | Sends draft **12** exactly as Claude wrote it |
| `send 12 Thanks, Thursday works for me` | Sends **your** wording (inner newlines preserved) |
| `discard 12` | Marks draft 12 as discarded |
| `drafts` | Lists the drafts waiting for you |
| `summary` | Runs the daily summary right now |
| `help` | Shows the command list |

Commands are case-insensitive. Wrong ids, already sent drafts and already
discarded drafts get a friendly explanation — never an exception.

Messages that contain a new email look like this:

```
📧 *New email*
From: alice@example.com
Subject: Lunch tomorrow?

Preview: Are we still on for lunch tomorrow?

*Draft reply (id 7):*
Sure, lunch tomorrow works for me!

Reply `send 7` to send it as-is,
`send 7 <your text>` to send your own version,
or `discard 7` to drop it.
```

If Claude judges an email to be a scam it sends a warning instead of a draft:

```
⚠️ *Suspicious email - not drafted*
From: scam@example.net
Subject: Verify your account
...
Reason: asks for credentials
```

---

## Admin dashboard (email-response activity)

The app includes an optional, credential-protected admin frontend to review **all
email-response management activity**:

* incoming email snapshots and processing events,
* generated drafts and their corresponding emails,
* WhatsApp approval commands,
* **sent responses/final text linked to the email and draft** (including edited
  `send <id> <text>` responses),
* discards, suspicious/no-reply decisions, and daily summaries.

### Access
* `GET /admin/login` — sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`
* `GET /admin/dashboard` — activity log, drafts/responses, email snapshots
* `GET /admin/draft/{id}` — draft + final response + corresponding email

The admin UI is enabled only when both `ADMIN_PASSWORD` and `ADMIN_SECRET_KEY`
are set. Sessions use signed cookies (`HttpOnly`, `SameSite=Lax`). Run behind
HTTPS in production and set a strong secret.

Activity/response data is persisted in the configured database (`email_snapshots`,
`activity_log`, and `drafts` including `final_text`/status/timestamps).

---

## MCP endpoint / Claude Desktop

The MCP server lives at `/mcp` and uses the **streamable HTTP** transport.
Every request must carry the bearer token:

```
Authorization: Bearer <MCP_API_KEY>
```

> **Note on the MCP SDK.** The project pins `mcp==2.3.0`. In this release the
> old `FastMCP` class was renamed to **`MCPServer`** and now lives in
> `mcp.server.mcpserver` (`from mcp.server.fastmcp import FastMCP` raises a
> `ModuleNotFoundError` on `mcp` 2.x). `app/mcp_server.py` therefore builds
> the server through `create_mcp_server()` and exposes it as an ASGI app via
> `create_mcp_asgi_app()`.

Two implementation details are worth knowing:

* **The endpoint is mounted with a small ASGI middleware**
  (`app.main.MCPBearerAuthMiddleware`), not with `starlette.mount()`. A plain
  `Mount` answers `/mcp` with a `307` redirect to `/mcp/`, which some MCP
  clients do not follow. The middleware forwards the request to the MCP ASGI
  app directly, so **`/mcp` works without a trailing slash** (both spellings
  are accepted).
* **DNS-rebinding protection is disabled** (`TransportSecuritySettings` with
  `enable_dns_rebinding_protection=False`). The endpoint has to be reachable
  through ngrok or a reverse proxy, so the `Host` header cannot be restricted
  to `localhost`. The bearer token is the authentication mechanism.

The transport is created with `json_response=True`, i.e. the server answers
with a single JSON response instead of an SSE stream.

The MCP session manager runs inside the FastAPI **lifespan**
(`async with app.state.mcp_server.session_manager.run():`), so the server is
started before the first request and shut down cleanly with the app.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "email-whatsapp": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
    }
  }
}
```

> Use the **exact** URL including the scheme. `http://127.0.0.1:8000/mcp`
> (no trailing slash) is the safest choice — do not write `/mcp/`.

Available tools:

| Tool | Description |
| --- | --- |
| `list_emails(query="is:unread in:inbox", limit=10)` | Search the mailbox |
| `read_email(email_id)` | Read one email (full body) |
| `draft_reply(email_id)` | Ask Claude for a draft and store it as **pending** |
| `send_email_reply(email_id, text)` | Send a reply in the original thread |
| `send_whatsapp(text)` | Message the owner on WhatsApp |
| `list_pending_drafts()` | Drafts waiting for approval |

> `send_email_reply` bypasses the WhatsApp approval loop *by design*: it is
> the owner talking to their own MCP client. If you would rather have a single
> approval path for everything, do not expose this tool to untrusted clients.

---

## The 24 hour WhatsApp window

Meta only lets a business send **free-form** messages to a user within **24
hours** of that user's last message. Outside that window the API rejects the
message with an error like *"more than 24 hours have passed"* (code
`131047`/`131056`).

Consequences and the implemented behaviour:

* The daily summary is scheduled at `SUMMARY_HOUR` — if you did not message
  the agent in the previous 24 hours, the free-form message is rejected.
* The agent detects this specific error (`WhatsAppTemplateRequiredError`),
  logs it clearly and falls back to `send_template()` with the approved
  template `WHATSAPP_TEMPLATE_NAME` (`daily_summary_opener` by default).
* **You must create and approve that template in the Meta dashboard** — the
  template body should contain a placeholder such as `{{1}}` for the summary
  text. Until it exists, the fallback fails and the failure is logged with a
  clear hint.
* Practical tip: message the agent once a day (e.g. reply `summary`) — that
  re-opens the 24 hour window and every free-form message works again.

---

## Tests and linting

```bash
pip install -r requirements.txt      # includes the dev dependencies
pytest -q                            # run the test suite
ruff check .                          # lint
ruff format .                         # optional formatting
```

The suite mocks Gmail, WhatsApp and Anthropic completely — no network access
and no credentials are needed. It covers:

* command parsing (`send 3`, `send 3 edited text`, wrong id, already sent,
  discard, unknown command, unauthorised sender)
* webhook rejects bad/missing signatures and unknown phone numbers
* duplicate webhook deliveries are processed exactly once
* `NO_REPLY_NEEDED` and `SUSPICIOUS:` handling
* the new-mail flow creates exactly one draft per email and is idempotent
* MCP requests without the bearer token are rejected with `401`

`tests/test_end_to_end.py` adds a **full-stack** suite on top of that. It
boots the real FastAPI application (real lifespan, real scheduler, real
SQLite file) and fakes only the three external boundaries:

| Boundary | How it is faked |
| --- | --- |
| Google REST API | a fake `googleapiclient` service object |
| Meta Graph API | `respx` (the WhatsApp client speaks plain `httpx`) |
| Anthropic API | `httpx2.MockTransport` (the SDK v2 speaks `httpx2`) |

It then drives the real flows end to end — email arrives → draft → WhatsApp
announcement → the owner replies `send <id>` → the reply is sent with the
right `To`, `Re:` subject, `In-Reply-To`, `References` and `threadId`, and
with no `Cc`/`Bcc` — plus the security rules (a stranger, a missing
signature and a duplicate delivery can never send an email) and the
prompt-injection defence.

---

## Docker & PostgreSQL

The project is containerized with Docker Compose:

* **`postgres`** (`postgres:16-alpine`, container `email-whatsapp-postgres`) —
  PostgreSQL database with a named volume for durable data.
* **`app`** (built from `Dockerfile`, container `email-whatsapp-app`) — FastAPI
  app + scheduler + MCP + WhatsApp webhook + admin UI, depends on healthy
  Postgres and mounts an app-data volume for OAuth/SQLite-fallback files.

### Named volumes
* `email_whatsapp_pgdata` — PostgreSQL data (`/var/lib/postgresql/data`)
* `email_whatsapp_appdata` — app data (`/srv/app/data`, e.g. `token.json`,
  SQLite fallback)

### Configure
```bash
cp .env.example .env
# set required API/WhatsApp/MCP values and admin credentials
# DATABASE_URL defaults in compose to: postgresql://email_agent:...@postgres:5432/email_agent
```

### Start PostgreSQL (required for Docker persistence)
```bash
docker compose up -d postgres
docker compose ps
```

### Start the full stack
```bash
docker compose up -d --build
docker compose logs -f app
```

The app uses `DATABASE_URL` (PostgreSQL) when set. Compose sets it for the
`app` service by default. If `DATABASE_URL` is empty, the app falls back to
SQLite at `DB_PATH` (useful for local development/tests).

### Gmail OAuth token in Docker
Provide `token.json` (from `scripts/gmail_auth.py`) to the app container, for
example by mounting it or copying it into `email_whatsapp_appdata`:

```bash
# example: copy into running container
docker cp token.json email-whatsapp-app:/srv/app/data/token.json
```

### Standalone image (without Compose)
```bash
docker build -t email-whatsapp-agent .
docker run --rm -p 8000:8000 --env-file .env -v "$PWD/data:/srv/app/data" email-whatsapp-agent
```

---

## Security notes

| Requirement | How it is enforced |
| --- | --- |
| No email without your explicit `send` command | `services._handle_send` is the only path to `gmail.send_reply`; it is reachable from the WhatsApp command handler, which validates the sender against `MY_WHATSAPP_NUMBER`. |
| Webhook signature verification | `routes_whatsapp.verify_signature` recomputes `HMAC-SHA256` over the raw body with `WHATSAPP_APP_SECRET` and compares it in constant time. A mismatch returns `403`. |
| Sender allow-listing | Every inbound message is compared to `MY_WHATSAPP_NUMBER` (digits only). Everything else is ignored. |
| Idempotency | `processed_wa_messages` stores the WhatsApp message id, so a redelivery is processed only once. |
| Email content is untrusted data | Bodies are wrapped in `<email>…</email>` delimiters, the closing tag is neutralised inside the body, and the model is told explicitly that the content is data, not instructions. Nothing from an email is ever executed. |
| No secrets in code or logs | `logging_config.SecretRedactingFilter` scrubs the API key, the WhatsApp token, the app secret and the MCP key from every log record. Bodies and tokens are never logged. |
| Secrets out of git | `.env`, `token.json`, `client_secrets.json` and `*.db` are in `.gitignore`. |
| MCP needs the bearer token | `main.create_app` installs a middleware that rejects `/mcp` requests without `Authorization: Bearer <MCP_API_KEY>` (constant-time comparison). |
| Replies go only to the original sender | `gmail_client.send_reply` sets `To` to the original sender and never sets `Cc`/`Bcc`; the reply stays in the original thread via `In-Reply-To`, `References` and `threadId`. |

---

## Troubleshooting

**`Missing required environment variable(s): …`**
The `.env` file is missing or incomplete. `cp .env.example .env` and fill in
the required values. The app refuses to start rather than failing later.

**`Gmail token file … does not exist`**
Run `python scripts/gmail_auth.py` first.

**`The Gmail access token is invalid or expired`**
The refresh token was revoked. Delete `token.json` and run the auth script
again.

**`The Gmail token is missing required scope(s): …gmail.send`**
The token was created without the send scope. Re-run `scripts/gmail_auth.py`
and accept all requested permissions.

**Meta returns `403` on the webhook**
Either `WHATSAPP_VERIFY_TOKEN` does not match the one configured in the Meta
dashboard, or the callback URL is wrong. The verification handshake is
`GET /webhook/whatsapp?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…`.

**Meta returns `401`/`403` on the POST**
The `X-Hub-Signature-256` header did not match. Check `WHATSAPP_APP_SECRET`
(the *app secret*, not the access token) and make sure no proxy rewrites the
request body.

**The agent does not react to my WhatsApp messages**
Check `/health`, then check the logs:

* *Ignored a message from an unknown number* → `MY_WHATSAPP_NUMBER` does not
  match. Compare with the `from` field of the incoming payload.
* *Ignored a duplicate webhook delivery* → the message id was already handled.
* Nothing at all → the webhook URL is wrong or ngrok is not running.

**The daily summary never arrives**
* Outside the 24 hour window? See [the section above](#the-24-hour-whatsapp-window).
* Check `SUMMARY_HOUR`/`SUMMARY_TIMEZONE`; `/health` shows the next run time
  of every job.

**`more than 24 hours have passed` in the logs**
Expected outside the window. Approve the `daily_summary_opener` template in
the Meta dashboard, or message the agent once to re-open the window.

**Claude errors: `rate_limit_error` / `overloaded_error`**
The wrapper retries three times with exponential backoff. If it persists,
lower `POLL_INTERVAL_MINUTES` or switch to a cheaper model via `CLAUDE_MODEL`.

**An MCP client gets `401 Unauthorized`**
The `Authorization: Bearer <MCP_API_KEY>` header is missing or does not match
`MCP_API_KEY` exactly (the comparison is constant-time, so a typo simply
fails). Check the header in your client configuration and make sure you did
not include the word `Bearer` twice.

**An MCP client gets `404` on `/mcp`**
Use `http://127.0.0.1:8000/mcp` (no trailing slash). Both spellings are
handled, but some clients build the URL incorrectly and append their own
path. `GET /health` also reports the effective `mcp_path`.

**Nothing happens on the first run**
By design: the existing unread mail is seeded as "seen" so you are not
flooded. Send a new email to yourself (or `mark as unread` an old one) to
trigger a draft. Delete the `data/agent.db` file to re-seed from scratch.

---

## Design decisions and assumptions

* **Project layout** – the tree from the brief is the repository root: the
  code lives directly in `app/`, `scripts/` and `tests/` of this directory.
* **MCP SDK 2.x** – the brief asked for `FastMCP`, but the pinned SDK
  (`mcp==2.3.0`) renamed that class to **`MCPServer`** (`mcp.server.mcpserver`);
  the old import path no longer exists. The code uses the new name — the
  public behaviour (streamable HTTP at `/mcp`, six tools) is unchanged.
* **MCP is mounted with ASGI middleware, not `Mount`** – a Starlette `Mount`
  redirects `/mcp` → `/mcp/`, which some MCP clients mishandle. The custom
  middleware checks the bearer token and forwards to the MCP ASGI app
  directly, so both spellings work. DNS-rebinding protection is disabled on
  purpose (tunnels/reverse proxies break the `Host` allow-list); the bearer
  token is what protects the endpoint.
* **`app/prompts.py` is a placeholder.** The real prompts were not provided
  yet, so a minimal, functional implementation with the exact same interface
  (`build_prompt(mode)` for `draft`, `summary`, `agent`) was created. Replace
  the file with your own — the rest of the code does not need to change. The
  prompt is used as the **system** prompt; the email payload is passed
  separately as untrusted user content, which is what keeps prompt injection
  from the email side contained.
* **Special markers** – the draft prompt asks Claude to answer with exactly
  `NO_REPLY_NEEDED` or `SUSPICIOUS: <reason>`. The parser looks at the first
  line of the answer, so trailing commentary is tolerated.
* **Body truncation** – email bodies are cut at `MAX_EMAIL_BODY_CHARS`
  (6000 characters) with a `…[truncated]` marker.
* **Long WhatsApp messages** are split at 4000 characters, preferring line
  breaks.
* **Database** – **PostgreSQL is primary** when `DATABASE_URL` is set (Docker
  Compose uses a `postgres:16-alpine` container with the `email_whatsapp_pgdata`
  named volume). When `DATABASE_URL` is empty, the app falls back to plain
  `sqlite3` (WAL mode) at `DB_PATH` so local development and tests run without
  PostgreSQL. A unique index on `drafts.email_id` guarantees *at most one draft
  per email*. The DB also stores `email_snapshots` and `activity_log` records
  (including sent response text linked to the corresponding email/draft) for the
  admin dashboard.
* **Blocking calls** – the Google API client is blocking, so every call runs
  through `asyncio.to_thread`. SQLite statements are microseconds long and are
  called inline.
* **Error notices** – failures are logged and pushed to WhatsApp, but the same
  error key is only reported once every `ERROR_NOTICE_COOLDOWN_MINUTES`
  (30 minutes) so a broken dependency cannot spam you.
* **Webhook processing** – the HTTP handler verifies the signature, checks the
  sender and claims the message id *synchronously* (so concurrent
  redeliveries cannot both be processed), then executes the command in a
  FastAPI background task so Meta gets its `200` immediately.
* **`summary` command** – runs the daily summary and does not send an extra
  confirmation message (the summary itself is the answer).
* **Claude Desktop** – the official client historically only spoke stdio;
  recent versions support remote MCP servers over HTTP with custom headers.
  The configuration above works with those versions. If yours is stdio-only,
  point it at a small `mcp-remote` bridge instead.
* **Python version** – the code targets 3.11+; the only 3.11+ specific
  behaviour used is `zoneinfo` (with the `tzdata` package installed on
  Windows).

---

## Licence

Personal project – use it, adapt it, break it.
