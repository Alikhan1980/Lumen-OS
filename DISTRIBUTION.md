# Shipping this to other people

You set up Google Cloud **once**. Everyone you give the app to just signs in.

They need: nothing installed, no Google Cloud account, no Python. They
double-click an `.exe` and click through a Google sign-in.

## Who pays

**Each recipient, with their own AI provider key.** This is not configurable.
The app has no API key of its own, `build.ps1` has no switch to put one in, and
there is no code path that could fall back to one. A recipient who has not
connected a provider gets a setup screen and an agent that does nothing.

That is the deliberate design, and it is what makes this safe to hand out: your
key cannot leak through an executable you gave someone, and nobody can spend
your money.

On first run each person picks OpenAI, Anthropic or Google Gemini, pastes a key
from their own account, and the app stores it in *their* Windows Credential
Manager. Their requests go from their machine straight to that provider. You
see nothing, pay nothing, and run no server.

**If you want to pay for someone**, do it at the provider rather than in this
app — it needs no code and keeps the same safety property. In the Anthropic
Console or the OpenAI platform, create a separate workspace/project per person,
set a monthly spend limit on it, and hand each of them a key scoped to their
own. They paste it once like anyone else, and you keep per-person cost
visibility and can revoke one person without touching the others.

What you should **not** do is share one key between several people. It cannot
be revoked individually, spending cannot be attributed, and any one of them can
read it out of their own credential store.

---

## The constraint you need to know first

The agent uses `gmail.modify` and full `drive` access. Google classifies both as
**restricted scopes** — the strictest tier. That means:

| Publishing status | Users | Sign-in experience | Verification |
|---|---|---|---|
| **Testing** | 100, each added by email | clean | none, but **refresh tokens die after 7 days** — everyone re-signs-in weekly |
| **In production, unverified** ← use this | **100 total** | "Google hasn't verified this app" warning, click through once | none |
| **In production, verified** | unlimited | clean | app review **+ annual third-party CASA security assessment** (weeks, and it costs real money) |

**Publish it unverified.** The 100-user cap is a hard ceiling, and the warning
screen never goes away, but you pay nothing and wait for nobody. For a tool you
hand to colleagues or friends this is the right trade.

If you ever need past 100 users, the only route that avoids the CASA audit is
dropping to narrower scopes — and there isn't a good one for reading mail, since
`gmail.readonly` is restricted too. Plan on 100 or plan on the audit.

---

## 1. Create the OAuth client (once)

Follow **[SETUP.md](SETUP.md) sections 3a–3d** — same steps, with two
differences:

- On the consent screen (3c), click **Publish app** so users aren't capped at
  the 7-day token expiry.
- Save the downloaded JSON to the **project root**, not `credentials/`:

  ```
  C:\Users\mamma\ai-agent\client_secret.json
  ```

  That's the file `build.ps1` bakes into the executable. It's gitignored.

### Is shipping the client secret safe?

Yes, for a Desktop OAuth client. Google's own documentation states that
installed apps cannot keep a secret, and their security model doesn't rely on
it — the loopback redirect is what actually protects the flow. Anyone who
extracts it can only make sign-in prompts that say *your app's name*; they
cannot touch any user's data, because that requires a token each user
individually granted on their own machine.

What you must **not** ship is `credentials/token.json` (one user's live access)
or `.env` (your own settings). Both are gitignored and neither is bundled.

Your AI provider key is not at risk here, because it was never in the project
in the first place — it lives in your Windows Credential Manager, which does
not travel with a build.

---

## 2. Build

```powershell
.\build.ps1
```

Produces `dist\WorkspaceAgent.exe` — about 40 MB, containing Python, every
dependency, and your OAuth client. No AI API key is in it, and there is no flag
that would put one there. Verify before sending:

```powershell
.\dist\WorkspaceAgent.exe --check
```

You want `OK   Google OAuth client found`. If it says `MISS`, the
`client_secret.json` wasn't in the project root when you built.

`--check` on your own machine will also report your own connected providers,
because it reads *your* credential store. That is a property of the machine, not
of the build — a recipient running the same `.exe` sees
`MISS  This agent needs an AI provider…` until they connect their own.

---

## 3. Send it

Just the `.exe`. Nothing else.

Tell recipients:

> Double-click it. It'll ask you to connect an AI provider — pick OpenAI,
> Anthropic or Google Gemini, whichever you have or want an account with, and
> paste in a key from that account. It's pay-per-use and billed to you by them;
> budget a few dollars to start. Then it opens your browser to sign in to
> Google. You'll see a warning saying the app isn't verified — that's expected,
> click **Advanced → Go to (name) (unsafe)**.

Then add each person's Google address under **Audience → Test users** in your
Cloud project if you left it in Testing. If you published, you don't need to.

---

## Who is who

Each copy is **single-account**, and identity is settled by which machine and
Windows profile the process runs in:

```
Your Cloud project ─── one OAuth client ─── identifies the APP to Google
                                │
        ┌───────────────────────┼───────────────────────┐
   Alice's PC              Bob's PC                Carol's PC
   own sign-in             own sign-in             own sign-in
   own refresh token       own refresh token       own refresh token
   %APPDATA%\WorkspaceAgent\credentials\token.json (per Windows user)
```

Your OAuth client says *"this app is WorkspaceAgent"*. It carries no user
identity. When Alice approves the consent screen, Google mints a refresh token
bound to **her** account and it is written to **her** `%APPDATA%`. Every Google
call the agent makes uses `userId="me"`, which resolves to the owner of the
token being presented — so Alice's copy has no credential capable of reaching
Bob's mailbox.

Consequences worth being explicit about:

- **You never see their Google tokens.** Tools run on their machine and talk to
  Google directly.
- **You never see their prompts, their answers, or their AI key.** Nothing
  routes through you: their machine talks to their chosen AI provider directly.
  There is no server of yours in the path and no way to add one.
- **Two Windows users on one PC are two separate agents**, because `%APPDATA%`
  and the Credential Manager are both per-profile — including their API keys.
- **Two people sharing one Windows login would share one token.** `/whoami`
  shows the active account and `/signout` clears it — and in the chat window,
  the **Switch account** and **Sign out** buttons in the header do the same. If
  that's a real scenario, give them separate Windows profiles.
- **You cannot revoke anyone's access to the AI**, because you never granted
  it — their key is theirs. Removing them from Test users blocks new Google
  sign-ins, not existing tokens, and each user revokes their own Google grant
  at <https://myaccount.google.com/permissions>. If you funded someone by
  issuing them a key from a workspace you own, revoke it at the provider.

The banner shows the active account on every launch:

```
╭───────────────── Google Workspace Agent ──────────────────╮
│ acting as alice@example.com                               │
│ calendar 6  contacts 2  drive 6  files 3  gmail 9  tasks 5│
╰───────────────────────────────────────────────────────────╯
```

---

## Why no AI key is ever bundled

A PyInstaller bundle is trivially unpacked, and an AI API key is a bearer
credential: whoever extracts it spends your money, with nothing tying usage to
a person and no way to cut off one user. That is the opposite of the OAuth
client secret above, which is safe to ship precisely because it grants nothing
on its own.

So there is no way to do it. `build.ps1` has no switch for it, `.env` is not
bundled, and the app reads AI keys only from the credential store of the
Windows account it is running as — which on a recipient's machine is empty
until they put something in it.

The app enforces the same thing at runtime, not just at build time. Every AI
request goes through `ProviderManager.require_active()`, which raises unless
the user has configured a provider of their own; there is no branch anywhere
that reaches for a key belonging to whoever built the app. The test suite
asserts this directly — it greps the whole repository for live-looking keys and
for fallback markers, and it drives the agent with nothing configured to prove
it refuses.

---

## Where user data lives

| | Path |
|---|---|
| **AI provider API keys** | **Windows Credential Manager** (not a file) |
| Provider choices (no keys) | `%APPDATA%\WorkspaceAgent\providers.json` |
| Agent settings | `%APPDATA%\WorkspaceAgent\.env` |
| Google token | `%APPDATA%\WorkspaceAgent\credentials\token.json` |
| Signed-in address | `%APPDATA%\WorkspaceAgent\credentials\account.json` |
| Downloads / file tools | `%APPDATA%\WorkspaceAgent\workspace\` |
| Browser downloads | `%APPDATA%\WorkspaceAgent\workspace\downloads\` |
| Browser profile (cookies, site logins) | `%APPDATA%\WorkspaceAgent\browser-profile\` |
| Search / browser activity log | `%APPDATA%\WorkspaceAgent\activity.log` |

Deleting that folder resets a user completely. Uninstalling is deleting the
`.exe` and that folder.

> **The browser tools are not in the packaged build.** Playwright drives a
> Chromium it downloads separately (~150 MB), which PyInstaller does not bundle;
> a recipient of the `.exe` gets search and everything Google, and the browser
> tools report that Chromium is missing when called. Shipping them means either
> having recipients run `playwright install chromium`, or bundling the browser
> and pointing `PLAYWRIGHT_BROWSERS_PATH` at it in the build.

---

## Updating

Rebuild, resend the `.exe`. User data lives outside the app, so nobody re-does
setup — they keep their provider key and their Google sign-in.

If you change `SCOPES` in `agent/tools/google_auth.py`, existing tokens become
insufficient. The code detects this and re-runs sign-in automatically, but users
will see the consent screen again. Also add any new scope to the OAuth consent
screen in Cloud Console, or Google refuses it.

> **Upgrading from a build that predates multi-provider support.** Anyone
> coming from a version that kept an Anthropic key in `%APPDATA%\WorkspaceAgent\.env`
> is asked to connect a provider once, and the key moves into their Credential
> Manager. Their Google sign-in, workspace and reminders are untouched. Tell
> them to delete the `ANTHROPIC_API_KEY` line from that `.env` afterwards — the
> app no longer reads it in a packaged build, so it is a stale secret sitting
> in a plaintext file.

---

## Antivirus

PyInstaller executables are unsigned and sometimes trip SmartScreen or
heuristic AV — a known false positive with single-file Python builds. Options:
tell recipients to click **More info → Run anyway**, or buy a code-signing
certificate (a few hundred dollars a year) if you're distributing widely enough
for it to matter.
