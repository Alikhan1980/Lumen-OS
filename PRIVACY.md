# Privacy

This app runs on your computer. As it ships there is no account to create, no
server of ours in the path, and nothing to sign up for. What follows is a
description of where your data actually goes, written to match what the code
does rather than to sound reassuring.

There is a second way to run it. The app can be pointed at an **accounts
server**, which is what makes it usable by more than one person, and that
changes these answers substantially — conversations and reminders are then kept
in a database rather than on your machine. That mode is off unless
`LUMEN_API_URL` is set; when it is off, the app has no sign-in screen and never
contacts an accounts server. Everything from here to *Removing everything* is
about the app on your computer and is true either way. [If you sign in to an
accounts server](#if-you-sign-in-to-an-accounts-server), at the end, is the
part that only applies when you do.

## You bring your own AI API key

This app has no AI API key of its own and no way to obtain one. Before it will
do anything you connect a provider — OpenAI, Anthropic or Google Gemini — using
a key from an account you hold with that provider.

That means:

- **You pay the provider directly** for your own usage, at their published
  prices and under their terms. This app adds nothing to that and takes no cut.
- **Your relationship is with them.** Their privacy policy, data-retention
  policy and terms of service govern what they do with what you send. Read the
  one for whichever provider you choose.
- **Nobody else can see your usage**, cap it, meter it, or bill you for it,
  because the requests go straight from your computer to the provider. This
  stays true with an accounts server: AI requests do not pass through it.

## Where your API key is stored

In your operating system's own credential store:

| Platform | Store |
|---|---|
| Windows | Credential Manager, encrypted per Windows user account |
| macOS | login Keychain |
| Linux | Secret Service (GNOME Keyring, KWallet) via `secret-tool` |

The key is **not** written to `.env`, to `providers.json`, to any configuration
file, to the activity log, or into the built executable. `providers.json` next
to your other app data holds only choices — which provider is active, which
model each one uses, and whether fallback is on.

The app never displays a stored key. Anywhere one is shown it appears as its
last four characters (`••••••••••••••••abcd`), which exists only so you can
tell two keys apart. There is no screen, command or API endpoint that will read
a stored key back out — including the local web UI, whose provider endpoints
return the masked form and nothing else.

**Limitation, stated plainly:** if a machine has no credential store at all,
the app refuses to store a key rather than inventing somewhere to put it. You
can override that with `AGENT_ALLOW_INSECURE_KEYSTORE=1`, which writes the keys
to a file in your app data folder. That file is base64-encoded, which is *not*
encryption and is not claimed to be — with no OS keystore there is nowhere to
put an encryption key that an attacker who can read that file could not also
read. The app labels the keys as unprotected everywhere it shows them while
this is on.

## Where your API key is sent

To the provider it belongs to, and to nowhere else.

Each request goes from your computer straight to that provider's API, with the
key in the authentication header — `Authorization: Bearer …` for OpenAI,
`x-api-key` for Anthropic, `x-goog-api-key` for Gemini. Keys are never put in a
URL, where they would end up in server logs and browser history.

Specifically, your API key is **not**:

- uploaded to a database,
- sent to any server belonging to whoever built or distributed this app,
- included in analytics, telemetry or crash reports — this app collects none
  of those and sends nothing anywhere on its own initiative,
- written to `activity.log` or any other log,
- included in an error message shown to you or copied into a bug report,
- committed to git.

Error text coming back from a provider is scrubbed of anything key-shaped
before it is displayed or logged, because providers sometimes echo the
offending key back inside an authentication failure.

## What is sent to your AI provider

Your prompts, and whatever the agent reads while working on them.

This is the part worth understanding. The agent has tools that read your Gmail,
Drive, Calendar, Contacts and Tasks, fetch web pages, and drive a browser. When
it uses one, **the result is sent to your AI provider as part of the
conversation**, because that is how the model reads it. If you ask it to
summarise an email, that email's contents go to the provider. If you ask it to
find something in a Drive document, that document's text goes to the provider.

That is inherent to what the app does, not a design choice that could be
avoided while keeping the feature. The mitigation available to you is choosing
which provider you trust with it, and the app makes the active provider visible
on every screen for that reason.

The agent asks before it acts outwardly — sending mail, sharing a file,
deleting something, buying something — but reading is what it does constantly
and is not individually confirmed.

## What is stored on your computer

In your app data folder (`%APPDATA%\WorkspaceAgent` on Windows when packaged,
the project folder in a source checkout):

| | |
|---|---|
| `credentials/token.json` | your Google OAuth token |
| `credentials/account.json` | the email address you signed in as |
| `providers.json` | provider choices. No keys. |
| `reminders.db` | your reminders |
| `workspace/` | files the agent reads, writes and downloads |
| `activity.log` | searches, page fetches and browser actions, rotated at 1 MB |
| `browser-profile/` | the browser's cookies and logins, if you use the browser tools |

`activity.log` exists because web and browser activity leaves no other trail —
a sent email is in your Sent folder, but a page the agent opened is not
recorded anywhere else. It contains no credentials. Set `AGENT_LOG_LEVEL=OFF`
to stop writing it.

There is no crash reporting and no analytics, and nothing here is transmitted on
the app's own initiative. The one exception is deliberate and is described
below: signed in to an accounts server, some of this — conversations, reminders,
tasks, preferences — is kept there as well, because that is what makes it the
same account on a second computer.

## Google data

The Google sign-in grants this app access to your own Google account under the
scopes listed in SETUP.md. That access is used by the tools, on your computer.
Google data is sent to your AI provider when the agent reads it, as described
above; it is not sent anywhere else.

Revoke it at any time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions), or
with `/signout` to forget the token locally.

## Removing everything

- `/providers` → remove each provider. This deletes the key from your OS
  credential store.
- `/signout` — forgets the Google token on this computer.
- Delete the app data folder to remove reminders, the workspace, the activity
  log and the browser profile.

## If you sign in to an accounts server

Everything above still holds — but signing in means an account on somebody's
server, and that server keeps things. This section is what it keeps, taken from
the migrations in `supabase/migrations/` rather than from a description of them.

It applies only when the app is pointed at a deployment (`LUMEN_API_URL`). With
no accounts server there is no sign-in screen, no account, and nothing in this
section happens.

### What the server stores

| | |
|---|---|
| Your identity | Email address, a hash of your password (never the password), and whether the address is confirmed. Held by Supabase Auth; this app proxies it and never sees a stored hash. |
| Your profile | Display name, avatar URL if you set one, time zone, and whether you finished onboarding. |
| Your preferences | Reply style, whether thinking is shown, whether tools auto-approve, notification toggles. |
| Your conversations | Every message, in full — including what the agent's tools read while answering. See the warning below. |
| Your reminders and tasks | Title, notes, due time, recurrence, status, and a log of when each reminder fired, was snoozed, or was completed. |
| Your connected accounts | Which provider, which address, which scopes, and the OAuth tokens themselves — encrypted with AES-256-GCM under a key kept outside the database. |
| Security events | Sign-ins, signups, password changes and deletions, with your browser's user-agent string and a **truncated** IP — a /24 for IPv4, /48 for IPv6. Enough to spot an attack in progress, not enough to be a location history. |
| Rate-limit counters | Keyed by an HMAC of the email address, so the table is not a readable list of everyone who has ever tried to sign in. |

**The conversations row is the one to read twice.** Running without an accounts
server, everything the agent read while working — the emails it summarised, the
documents it searched — stayed on your computer once the answer was given. With
one, that content is written to the server as part of the conversation. It is
the same content that goes to your AI provider either way, but it is now also at
rest in a database somebody else administers.

### What the server does not store

- **Your AI provider key.** It stays in your own OS credential store, and AI
  requests still go straight from your computer to the provider. Turns are not
  run server-side — deliberately, and `server/routers/agent.py` says why.
- **Your Google data.** The server holds the *token* that can reach your Gmail,
  Drive and Calendar; it does not copy their contents.
- **Any token in readable form.** OAuth tokens are encrypted before they are
  written, and the table holding them is not reachable by a signed-in user at
  all — only by the server's own privileged role.

### Who can see it

Every table is protected by row-level security with a policy of "the rows whose
`user_id` is you", forced at the database rather than checked in application
code, so another signed-in user cannot reach your rows even if a query forgets
to filter. [AUTH.md](AUTH.md) is the argument for that in full, and
`select * from public.rls_gaps();` is the tripwire that proves no table was
added without a policy.

Whoever operates the deployment holds the database credentials and can therefore
read what is in it, except for the OAuth tokens, which need the separate
encryption key as well. No claim in this document can change that; it is what
running a server means.

### Getting it back, and getting rid of it

- **Export**: `GET /api/account/export` returns everything the account holds as
  JSON — profile, preferences, conversations, messages, tasks, reminders and
  connections.
- **Delete**: deleting the account re-checks your password, revokes each
  connected integration at the provider first — so the grant is gone from your
  Google account and not merely from the database — and then deletes the
  identity, which cascades every table. If a provider cannot be reached the
  deletion still proceeds and the response tells you which grants to remove
  yourself.

Deletion is immediate and there is no recovery window. Backups are the operator's
to describe.

### For whoever runs the deployment

<!-- These are not answerable from the code, and every one of them is a claim
     about how the service is run rather than how it is built. Fill them in
     before this document is shown to anybody, and delete this comment. -->

**This section is incomplete and must be completed before publishing.** The
answers depend on how the server is operated, not on what the code does:

- [ ] **Who operates it** — the name and contact address of whoever runs the
      deployment, and where they are.
- [ ] **Where it is hosted** — the Supabase project's region, and Supabase as a
      processor holding the database and the auth service.
- [ ] **Who sends the email** — the SMTP provider configured for confirmation
      and reset mail sees every address it delivers to.
- [ ] **How long things are kept** — conversations and messages have no
      expiry in the schema; they are kept until deleted. Security events can be
      pruned with `prune_auth_events(90)` but only if that job is actually
      scheduled. State what is true of this deployment.
- [ ] **Backups** — whether point-in-time recovery is on, and for how long a
      deleted account can still exist in a backup.
- [ ] **Legal basis and rights** — whatever applies where the service and its
      users are, plus how a request under it is made.

## Changes to this document

This describes the code as it stands. If the architecture changes in a way that
makes any statement here untrue, the statement is the bug.
