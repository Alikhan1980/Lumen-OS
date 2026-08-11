# Lumen OS

An AI agent that works inside your actual Google account, and out on the open
web — running entirely on your own computer. Ask it something in plain English and it searches your mail, reads your
Drive, checks your calendar, looks things up online, drives a real browser, and
acts — 53 tools across Gmail, Drive, Calendar, Contacts, Tasks, its own
reminders, web search, browser automation, and a local workspace folder, all
behind a single Google sign-in.

**You bring your own AI API key.** Pick OpenAI, Anthropic or Google Gemini,
paste in a key from your own account, and the agent talks to that provider
directly from your computer. This app ships with no API key of its own, has no
way to obtain one, and takes no cut — you pay your chosen provider for your own
usage, at their prices. Until you connect one, the agent does nothing.

```
you  anything from Sarah this week I haven't replied to?

  ▸ gmail_search {"query": "from:sarah newer_than:7d", "max_results": 10}
    ✓ 3 result(s)
  ▸ gmail_read_thread {"thread_id": "19a2c4f8b1e3d7a2"}
    ✓ 4 result(s)

agent Two of the three need a reply.

      • "Q3 budget review" (Tue 10:14) — she's asking you to confirm the
        Thursday 2pm slot. Your calendar is free then.
      • "Contract draft v3" (Wed 16:40) — she attached the redlined PDF and
        wants comments by Friday.

      The third was a calendar invite you already accepted.
```

## Quick start

**Download [the latest release](../../releases/latest)**, double-click
`WorkspaceAgent.exe`, and your browser opens to the chat window. Nothing to
install — no Python, no Google Cloud account.

Windows will show a blue "Windows protected your PC" box the first time, because
the executable isn't code-signed. Click **More info → Run anyway**.

> **The release build is capped at 100 users.** It carries a Google OAuth client
> that Google has not verified, and unverified clients using Gmail and Drive
> scopes are limited to 100 sign-ins — permanently, not per month. If sign-in
> refuses you, that cap is why. Building from source with your own Google Cloud
> project has no such limit.

### It runs on your computer, not on a server

The address bar says `localhost:8765`. That is your own machine talking to
itself — the agent, your keys and your data never leave it, and there is no
website to sign up for. Closing the window stops the agent.

### From source instead

```powershell
git clone https://github.com/Alikhan1980/Lumen-OS.git
cd Lumen-OS
.\run.ps1
```

`run.ps1` creates the virtualenv, installs dependencies and downloads Chromium
for the browser tools on first run. You'll need your own Google Cloud OAuth
client — **[SETUP.md](SETUP.md)** covers it, about 10 minutes.

On first run either path walks you through two things: connecting an AI provider
with your own API key, and the Google sign-in. `.\run.ps1 --check` tells you
what's still missing.

You do **not** need a `.env` file. Copy `.env.example` only if you want to
change agent behaviour (workspace folder, browser policy, web-search keys) —
your AI provider key does not go in it.

**Giving this to other people?** See **[DISTRIBUTION.md](DISTRIBUTION.md)** —
`build.ps1` produces a single `.exe` with your OAuth client baked in, so
recipients need nothing installed and no Google Cloud account of their own.
Each recipient connects their own AI provider on first run; no key of yours is
ever built into the executable.

## Bring your own key

The agent has no AI access of its own. On first launch it stops at a setup
screen and will not send a message, run a tool, or answer anything until you
connect a provider:

```
AI Provider Required

To use this agent, you must connect at least one AI provider.

  1. OpenAI          https://platform.openai.com/api-keys
  2. Anthropic       https://console.anthropic.com/settings/keys
  3. Google Gemini   https://aistudio.google.com/apikey
```

There is no skip, no guest mode, and no built-in key to fall back to.

### Getting a key

| Provider | Where | Notes |
|---|---|---|
| **OpenAI** | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) | Needs a payment method or prepaid credit on the platform account. A ChatGPT Plus subscription does **not** cover API usage. |
| **Anthropic** | [console.anthropic.com](https://console.anthropic.com/settings/keys) | Needs credit on the account. Separate from a Claude.ai subscription. |
| **Google Gemini** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Has a free tier with low rate limits; paid usage needs billing enabled. |

Paste the key when asked. It is checked against the provider before anything is
saved, so a typo is caught immediately rather than at the first message.

### Managing providers

```
you  /providers
```

or, without starting a chat:

```powershell
.\run.ps1 --providers
```

```
  ✓  OpenAI          Connected · active   ••••••••••••••••abcd   gpt-5.1
  ✓  Anthropic       Connected            ••••••••••••••••wxyz   claude-sonnet-5
  ○  Google Gemini   Not connected

  Keys are stored in: Windows Credential Manager — encrypted per Windows user account
  Active provider: OpenAI · gpt-5.1
  Automatic fallback to another provider: off
```

From there you can add a provider, replace a key, pick a model, test a
connection, switch which provider is active, or remove one. You can keep all
three connected at once and switch between them; adding one never requires
removing another. The same screen exists in the browser UI under **AI
providers**.

Removing a provider deletes its key from the credential store. If it was the
active one, the agent either switches to the single remaining provider or asks
you to choose — and if it was the last one, the agent locks and returns to the
setup screen.

### Automatic fallback

Off by default. Turned on, a request that fails because your provider is down,
rate-limited or unreachable is retried with another provider you have
connected — **which spends money on that account**. A rejected API key never
triggers a fallback, because another provider would not fix it.

### Where your key goes

Into your operating system's own credential store, and nowhere else:

| Platform | Store |
|---|---|
| Windows | Credential Manager, encrypted per Windows user account |
| macOS | login Keychain |
| Linux | Secret Service (GNOME Keyring, KWallet) via `secret-tool` |

It is never written to `.env`, to a config file, to a log, or into the built
`.exe`. The only place it is ever sent is the provider it belongs to, in the
authentication header of a request your own machine makes. There is no server
of ours in the path — see [PRIVACY.md](PRIVACY.md).

If a machine has no credential store at all (a headless Linux box, typically),
the agent says so and refuses rather than quietly inventing somewhere to put
the key. Setting `AGENT_ALLOW_INSECURE_KEYSTORE=1` opts into an unencrypted
file instead; the app then labels the keys as unprotected everywhere it shows
them.

## What it can do

| App | Tools |
|---|---|
| **Gmail** | search, read message, read thread, send (with attachments), draft (with attachments), label/archive/star, list labels, trash, profile |
| **Drive** | search, read (Docs → text, Sheets → CSV), create, share, download, trash |
| **Calendar** | list calendars, list events, search, create, update/reschedule, delete, find free time — with recurrence and clash detection |
| **Contacts** | search (saved + people you've emailed), list |
| **Tasks** | list task lists, list, create, complete, delete |
| **Reminders** | create, list, search, update, complete, snooze, delete — the app's own, with notifications |
| **Day view** | one merged agenda of calendar events, reminders and tasks |
| **Web search** | search (with a recency window and `site:` filter), fetch a page as readable text |
| **Browser** | open, navigate, read, click, type, select, scroll, download, upload, screenshot, close |
| **Local files** | list, read, write — confined to `workspace/` |

Things it handles well because the tools compose:

- *"summarize what came in overnight and draft replies to anything urgent"*
- *"find the Q3 budget sheet in Drive and tell me if we're over on marketing"*
- *"am I free Thursday afternoon? if so book 90 minutes for deep work"*
- *"who at Acme have I emailed, and when was the last time?"*
- *"download the contract from Drive, read it, and list the payment terms"*
- *"turn the action items from this thread into tasks due Friday"*
- *"find the best APIs for adding voice AI to my app, with sources"*
- *"look up this vendor before I reply to their email"*
- *"go to the supplier portal, download the latest invoice, and email it to Dan"*
- *"remind me every weekday at 6pm to work on my app"*
- *"what do I need to do tomorrow?"* — events, reminders and tasks in one list

Attachments join those up: anything in `workspace/` can be attached to a mail,
and both `browser_download` and `drive_download_file` put files there — so
"download it and send it on" is one chain with no manual step in the middle.
Gmail caps a message at 25 MB; past that the agent is told to share it from
Drive and send the link instead.

### Reminders

The app's own, not a view onto Google Tasks. They live in a SQLite database
beside your other data, and they **ring whether or not the app is open** — a
Windows scheduled task runs a one-second sweep every minute, so nothing depends
on the agent being alive at the moment a reminder is due. Turn it on from the
Reminders page, or with `--reminders-install`; it is per-user and needs no
administrator rights.

Reminders repeat (daily, weekdays, weekly on chosen days, monthly, yearly),
snooze, carry tags and notes, and keep a history so a repeating one has a record
of each occurrence rather than a single tick. A reminder that came due while the
machine was asleep still fires, and says how late it is. A repeating one that
missed three days fires once and says so, rather than three times.

Times are wall-clock: "every weekday at 6pm" is still 6pm the day the clocks
change. Give an IANA zone (`Europe/Berlin`) and the reminder carries it.

The **Reminders** page in the rail groups them into Overdue / Today / Tomorrow /
Upcoming with a Completed tab, and you can create, edit, snooze, tick off and
delete by hand. The agent creates them from chat with the same database
underneath — *"remind me tomorrow at 4pm to finish my homework"* is a reminder,
not a sentence about one.

### Calendar, reminders and tasks together

Three things hold commitments and they are not interchangeable: an **event**
occupies a slot, a **reminder** rings at a moment, a **task** is a to-do with no
time. Ask *"what do I need to do tomorrow?"* and `daily_agenda` merges all three
into one time-ordered answer — and still answers if Google is unreachable,
naming what it could not reach.

Creating or moving an event checks the slot first: if you are already busy the
event is **not** created and the clash comes back instead, so the agent tells you
rather than double-booking. `calendar_find_free_time` only offers daytime on
weekdays unless asked otherwise, because nobody means 3am by "when am I free".

### The web, and the browser

Two levels, and the agent picks. **Search and fetch** answer questions: it
searches, opens the results that matter, and cites the URLs, keeping what it
read on the web separate from what it already knew. **The browser** does things:
it drives a real Chromium through your own logins, so a page that needs a
session, runs on scripts, or has to be clicked through is still reachable.

Search needs no account — it falls back to DuckDuckGo. Add a Brave, Tavily or
Google Programmable Search key in `.env` for better results and higher limits;
whichever you set is preferred and the keyless backend stays as the fallback, so
a provider outage costs you a retry rather than the answer.

The browser window is visible by default. That is deliberate: you can watch what
it is doing, and when a site wants a password or a captcha the agent stops and
asks you to do it yourself in the window — it never types credentials. Logins
persist in a profile under the app's data folder, so you sign in to a site once.

## Two interfaces

**Chat window (default).** Double-click the desktop shortcut, or `Chat.bat`, or
`.\run.ps1 --web`. Opens a chat page in your browser — type in the box, press
Enter. Tool calls appear inline as they run, and anything gated shows an
approve/skip dialog with the full arguments. The header shows the signed-in
account with **Switch account** and **Sign out** beside it.

It binds to `127.0.0.1` only, and every request needs a key minted at startup
and passed through the URL the launcher opens. Nothing is exposed to your
network.

The window opens whatever state setup is in — a shortcut with no console
attached has nowhere to show a prompt, so it never waits on one. With no API
key connected it opens on **API Keys** and asks for one there; mail, calendar
and reminders work in the meantime, and the chat says what it is waiting for
instead of failing when you send. The requirement itself is unchanged: the
agent refuses to answer without a key, and refuses in the service layer rather
than in the screen.

**Terminal.** `.\run.ps1` for the CLI, or `.\run.ps1 -p "question"` for a
one-shot answer. Same agent, same tools. Both interfaces can sign in, sign out
and switch Google account; the remaining slash commands below are
terminal-only.

## Commands

| Command | |
|---|---|
| `/help` | command list and example prompts |
| `/tools` | every tool, which app it belongs to, which ones ask first |
| `/whoami` | which Google account the agent is acting as |
| `/thinking` | show or hide the model's reasoning as it works |
| `/clear` | start a fresh conversation |
| `/cost` | token usage and estimated spend this session |
| `/signin` | sign in, or switch to a different Google account |
| `/signout` | forget the Google account on this computer |
| `/exit` | quit |

And, outside the chat:

| Flag | |
|---|---|
| `--reminders-install` | let Windows fire reminders when the app is closed |
| `--reminders-status` | is that set up, and when did it last run |
| `--reminders-uninstall` | stop it; reminders still work while the app is open |
| `--notify` | fire anything due now and exit (what the scheduled task runs) |

Non-interactive:

```powershell
.\.venv\Scripts\python.exe -m agent -p "what's on my calendar tomorrow?"
```

## Safety model

Anything outward-facing or hard to reverse stops and asks you first, showing the
exact arguments:

```
╭─ Approve: gmail_send_email ─────────────────────────╮
│ to       sarah@example.com                          │
│ subject  Re: Q3 budget review                       │
│ body     Thursday at 2pm works for me. I'll bring…  │
╰─ y = run once · n = skip ───────────────────────────╯
  Run this? [y/N]
```

Gated: `gmail_send_email`, `gmail_trash_message`, `drive_share_file`,
`drive_trash_file`, `calendar_create_event`, `calendar_update_event`,
`calendar_delete_event`, `tasks_delete`, `browser_upload`, `delete_reminder`.
Reads never prompt, and neither does creating a reminder — that is what the user
just asked for. Completing and snoozing are reversible, so they go straight
through; **deleting** a reminder ends every future occurrence of a repeating one
and cannot be undone, so it asks.

Clicking is judged by what it is about to do rather than by the tool's name — a
link and a **Place order** button are the same call. So `browser_click` and
`browser_type` resolve the element first, and stop for approval when it looks
like a purchase, payment, subscription, deletion, or a form submission with
legal or financial weight, showing you the element and why it asked:

```
╭─ Approve: browser_click ────────────────────────────╮
│ page     https://shop.example.com/cart              │
│ element  Place order                                │
│ why      this looks like a purchase or payment      │
╰─ y = run once · n = skip ───────────────────────────╯
```

Refusing returns a refusal to the model rather than an error, and the prompt
tells it not to go looking for another route to the same outcome. Cookie and
consent banners are excluded, so the prompt stays meaningful.

`--yes` (or `AGENT_AUTO_APPROVE=true`) turns the gate off. Think before you use
it — that lets the agent send mail with no confirmation.

Other boundaries:

- **No permanent deletion.** The Gmail scope cannot bypass the trash. Trashed
  mail and files are recoverable for 30 days.
- **The file tools cannot leave `workspace/`.** Every path is resolved and
  checked against the workspace root, so `..` and absolute paths are rejected.
  Downloads land in `workspace/downloads/`. Mail attachments and browser uploads
  call that same guard — `resolve_in_workspace` — rather than reimplementing it,
  so nothing outside the workspace can be sent anywhere.
- **The web tools cannot reach your network.** `web_fetch` resolves the host and
  refuses private, loopback and link-local addresses, and only speaks http and
  https — so a link in an email cannot turn the agent into a probe of your LAN.
  `AGENT_BROWSER_ALLOWED_DOMAINS` pins the browser to a list of hosts if you
  want it narrower still.
- **Page content is data, not instruction.** The system prompt tells the agent
  that text on a page never changes its task, and to report anything that tries.
- **Credentials stay local.** Your AI provider key lives in the OS credential
  store and is sent only to that provider; your Google token lives in
  `credentials/`, which is gitignored, and goes only to Google. Neither is ever
  sent to infrastructure belonging to whoever built this. The agent does not
  type passwords into websites; it asks you to.
- **Drafts are preferred over sends.** The system prompt tells the agent that
  "write a reply" means draft, not send.
- **Everything the agent does off your account is logged.** Searches, fetches
  and browser actions go to `activity.log` beside your `.env`, rotated at 1 MB.

## Configuration

Your AI provider and model are **not** configured here — they are chosen in the
app (`/providers`), and the key goes to the OS credential store. Everything
below is optional and lives in `.env`:

| Variable | Default | |
|---|---|---|
| `AGENT_EFFORT` | provider default | `low`–`max`; ignored by providers with no such control |
| `AGENT_MAX_TOKENS` | `32000` | ceiling per response, reasoning included |
| `AGENT_SHOW_THINKING` | `false` | also togglable with `/thinking` |
| `AGENT_AUTO_APPROVE` | `false` | skip every confirmation prompt |
| `AGENT_WORKSPACE` | `./workspace` | where the file tools operate |
| `BRAVE_SEARCH_API_KEY` | — | better search than the keyless default; 2,000/month free |
| `TAVILY_API_KEY` | — | search built for agents, cleaner extracts |
| `GOOGLE_SEARCH_API_KEY` + `GOOGLE_SEARCH_ENGINE_ID` | — | Google Programmable Search; both needed |
| `AGENT_SEARCH_PROVIDER` | best configured | pin one of `brave`/`tavily`/`google`/`duckduckgo` |
| `AGENT_BROWSER_HEADLESS` | `false` | run the browser invisibly |
| `AGENT_BROWSER_CONFIRM_ALL` | `false` | ask before *every* click and keystroke, not only risky ones |
| `AGENT_BROWSER_ALLOWED_DOMAINS` | — | comma-separated hosts the browser may open; empty means anywhere |
| `AGENT_LOG_LEVEL` | `INFO` | detail in `activity.log`; `OFF` to stop writing it |

For development only, `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY`
are read in a source checkout, so a scratch account can be used without touching
the one you configured in the app. A key you stored in the app always wins over
these, and a packaged build ignores them entirely unless
`AGENT_ALLOW_ENV_KEYS=1` — so nobody can inherit a developer's key by accident:

```
your stored credential  →  environment variable (dev only)  →  no credential
```

## How it works

```
agent/
  __main__.py    CLI: chat loop, slash commands, --check / --auth / --providers
  core.py        the agent loop — stream, run tools, feed results back
  registry.py    @tool decorator → tool definitions + dispatch
  prompts.py     system prompt (kept byte-stable so the cache holds)
  config.py      paths and .env; splits bundled vs per-user data. No keys.
  onboarding.py  first-run: connect a provider, then Google
  provider_ui.py the terminal provider screen, shared with onboarding
  web.py         the browser chat window, provider settings, account switching
  ui.py          rich rendering, tool traces, approval prompts
  approvals.py   the channel a running tool uses to ask before acting
  logs.py        activity.log — what the agent did outside your Google account
  reminders.py   the reminders database: storage, recurrence, firing
  notify.py      Windows toasts, the sweep, and the scheduled task
  providers/     bring your own key — see below
    base.py           the AIProvider interface, normalised turns and errors
    catalog.py        the registry: the one list of who exists
    manager.py        credentials, the active choice, and the lock
    keystore.py       OS credential stores, one class each
    settings.py       providers.json — choices only, never a secret
    openai_provider.py  anthropic_provider.py  gemini_provider.py
  tools/
    google_auth.py   one OAuth flow, shared by every Google tool
    gmail.py  drive.py  calendar.py  contacts.py  tasks.py  localfiles.py
    websearch.py     search across four providers, and page → text
    browser.py       Playwright/Chromium: one browser on one worker thread
    reminders.py     the seven reminder tools, plus the merged daily agenda
scripts/
  selftest.py    502 offline checks — no API keys needed
  verify_web.py  live check of search, and the browser against a local test site
  verify_google.py  read-only probe of every Google scope
build.ps1        → dist\WorkspaceAgent.exe (see DISTRIBUTION.md)
launcher.py      PyInstaller entry point
```

### The provider layer

Nothing above `providers/` imports a vendor SDK. The agent builds one normalised
turn and hands it to whichever provider is active:

```
Agent
  ↓
ProviderManager        ← the lock: no configured key, no request
  ↓
AIProvider             ← the abstraction
  ↓
OpenAI · Anthropic · Gemini · …
```

`ProviderManager.require_active()` is the only supported way to obtain a
provider, and it raises `ProviderNotConfigured` unless the user has connected
one. That is why the requirement holds in the service layer rather than in a
screen: `agent.send()` calls it before touching history, tools or the network,
and the web server answers `423` on `/api/chat` independently. A future entry
point gets the same guard for free as long as it asks the manager.

The abstraction deliberately does not flatten the providers into a fake common
API. Two things keep the differences intact:

- **`Capabilities`** — the agent asks what a provider can do rather than
  assuming. Mid-conversation system messages, prompt caching, reasoning effort
  and thinking traces exist on some and not others; the loop branches on the
  capability, never on a provider id.
- **`Turn.raw`** — an assistant turn keeps the exact payload its own provider
  produced, so replaying history back to that provider is lossless (Anthropic's
  thinking signatures survive). Sent to a *different* provider after a switch,
  it is rebuilt from the normalised blocks instead.

What that translation actually involves, per provider:

| | OpenAI | Anthropic | Gemini |
|---|---|---|---|
| Auth | `Authorization: Bearer` | `x-api-key` (SDK) | `x-goog-api-key` header |
| Assistant role | `assistant` | `assistant` | `model` |
| System prompt | first message | `system` field, cached | `systemInstruction` |
| Mid-conversation system | none — folded into the user turn | newest models only | none — folded in |
| Tool arguments | JSON **string**, streamed in fragments | JSON object | JSON object |
| Tool result | its own `tool` message | a block in the user turn | `functionResponse` |
| Call ids | yes | yes | **none** — paired by name, ids synthesised |
| Tool schema | JSON Schema | JSON Schema | OpenAPI subset — unsupported keywords stripped |
| Reasoning | `reasoning_effort`, text not returned | adaptive thinking, summarised | thinking-token *budget* |

**Adding a provider** is one module and one line in `catalog.py`. Implement
`validate_key`, `catalog`, `capabilities` and `stream`, register the class, and
setup, settings, the model picker, the lock and the tests all pick it up
without further change. The self-test asserts this by registering a provider
that lives outside the package.

Provider failures are normalised to a small set of codes — `INVALID_API_KEY`,
`RATE_LIMITED`, `BILLING_ERROR`, `NETWORK_ERROR`, `PROVIDER_UNAVAILABLE`,
`MODEL_UNAVAILABLE`, `INVALID_REQUEST`, `CONTEXT_TOO_LONG`,
`PERMISSION_DENIED`, `UNKNOWN_PROVIDER_ERROR` — each with a message that says
what to do about it. Telling them apart is the point: OpenAI returns 429 both
for "slow down" and for "you are out of credit", and reporting either as a bad
key would send the user to replace a key that was working fine.

**Two storage roots.** Bundled data (the OAuth client) is read-only and ships
inside the exe. Per-user data (`.env`, Google token, workspace) lives in
`%APPDATA%\WorkspaceAgent` — which is what makes each Windows profile a separate
agent identity, and lets the app sit somewhere read-only. In a dev checkout both
collapse to the project root, so nothing changes while working on the code.

The loop is a manual streaming agentic loop over the provider abstraction:
stream a turn, and while the normalised `stop_reason` is `tool_use`, execute
every requested tool and return all results in a single user message. It also
handles `pause` (resume), `refusal` (roll the turn back cleanly), and
`max_tokens` (warn). Each provider maps its own vocabulary onto those four.

A few deliberate choices worth knowing if you extend it:

- **Adaptive thinking** is on with `display: "summarized"`, so `/thinking`
  shows real reasoning rather than a spinner. Thinking costs the same either
  way.
- **Prompt caching** has a breakpoint on the system prompt (which the tool
  definitions render ahead of, so both are cached) plus automatic caching of the
  growing conversation. A long tool-calling turn re-reads its own history at
  10% of input price instead of paying full freight each round.
- **Per-turn context** (the current date and timezone) never goes into the
  system prompt — editing that each turn would invalidate the cached prefix. On
  models that support mid-conversation `role: "system"` messages it uses that
  channel, which also can't be spoofed by text the agent reads out of an email.
  Sonnet 5 rejects those, so it folds the context into the user turn instead;
  both paths are covered by the test suite.
- **Tool order is sorted** in `registry.all_tools()` — a non-deterministic tool
  list would change the cache prefix on every run.
- **A reminder is claimed before it is announced.** The app's watcher thread
  and the scheduled task both sweep the same table; the claim is an
  `UPDATE ... WHERE notified_utc IS NULL` whose row count decides who owns the
  occurrence, so two processes cannot both toast it.
- **Reminder times are stored twice**, as a wall clock and as an instant.
  Recurrence advances the wall clock and re-resolves the instant, which is why
  a daily 5pm reminder is still 5pm the day the clocks change.
- **One browser, one thread.** Playwright's sync objects belong to the thread
  that created them, and the chat window runs every turn on a fresh thread, so
  `browser.py` funnels all Playwright work through a single worker thread it
  owns. That is what lets "open the report page" and, a message later, "now
  download it" be the same browser.
- **Element refs never repeat.** `browser_read` numbers what is on the page and
  stamps the number onto the element; the counter runs for the life of the
  session. Renumbering from 1 each read would let a ref quoted from an earlier
  message land on a different element and click the wrong thing silently —
  this way an out-of-date ref simply is not found, and the agent re-reads.

## Adding a tool

Drop a function in any module under `agent/tools/`:

```python
from ..registry import obj, tool

@tool(
    group="gmail",
    name="gmail_count_unread",
    description="Count unread messages in the inbox. Use this when the user "
                "asks how much mail is waiting rather than what it says.",
    schema=obj({"label": {"type": "string", "description": "Label to count within."}}),
    confirm=False,
)
def gmail_count_unread(label: str = "INBOX") -> dict:
    ...
    return {"count": n}
```

It is registered on import — add the module to `registry.load_all()` if it is
new. Return a JSON-serializable dict; raise on failure and the exception text
goes back to the model as an error result so it can adapt.

Descriptions do real work here. Say *when* to reach for the tool, not just what
it does — that is what drives correct selection.

## Testing

```powershell
.\.venv\Scripts\python.exe scripts\selftest.py     # offline, no keys, no network
.\.venv\Scripts\python.exe scripts\verify_web.py   # live: search + browser
.\.venv\Scripts\python.exe scripts\verify_google.py  # live: Google scopes, read-only
```

`selftest.py` is 502 checks covering schema validity, the tool-dispatch loop,
request shape, capability negotiation, context injection, both approval gates,
error propagation, refusal handling, turn rollback, workspace path traversal,
mail attachments, the reminders database (recurrence, snoozing, missed runs,
firing exactly once), the merged day view, calendar recurrence and free-slot
filtering, search provider selection and fallback, URL safety, the click
classifier, and the browser account routes.

On the BYOK side specifically it covers the full provider lifecycle (add all
three, keep them side by side, replace a key, switch the active one, remove
one, remove the last one), every normalised error code against each provider's
real error shapes, each provider's wire serialisation, the fallback policy, the
OS keystore round-trip, environment-variable precedence, and the security
properties: that a key never appears in a log, a URL, an error, `providers.json`,
the web API, or a `repr`; that no live key or developer-key fallback exists
anywhere in the repository; and that the agent refuses to run with no provider
configured, becomes usable when one is added, and locks again when the last one
is removed. It stubs every provider and Google, so it costs nothing, touches no
network, and needs no credentials.

`verify_web.py` is 53 live checks. Search and page reading go against the real
internet; the browser is driven against a throwaway site the script serves on
`127.0.0.1` — a page with a link, a form, a dropdown, a download and an upload —
so clicking and typing are checked against something that cannot change under
it. Nothing is bought, sent or deleted, and it tidies up after itself. Add
`--headed` to watch the browser work.

## Cost

Sonnet 5 is $3 per million input tokens and $15 per million output. A typical
inbox question runs around a cent; caching keeps multi-tool turns from
multiplying that. `/cost` shows the running estimate for the session. Switch
`AGENT_MODEL` to `claude-opus-5` for the most capable model at roughly double.
