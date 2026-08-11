# Setup — click by click

Everything below is done **once**. About 10 minutes, nearly all of it in Google
Cloud Console.

You need two credentials: an **AI provider API key** (pays for the AI, and is
billed to you by that provider) and a **Google OAuth client** (lets the app ask
people for access to their Google account).

> Only setting this up for yourself and already have an AI key? You can skip
> ahead — the app collects it on first run, and nothing has to be edited by
> hand.

---

## Step 1 — An AI provider API key

Pick **one** to start with. You can add the others later and switch between
them from inside the app; you are not locked in.

### OpenAI

1. Go to <https://platform.openai.com/api-keys>
2. Sign in, then **Create new secret key**. Copy it — it is shown once.
3. Add credit under **Settings → Billing**. A ChatGPT Plus subscription does
   **not** cover API usage; the platform account needs its own payment method
   or prepaid credit.

### Anthropic

1. Go to <https://console.anthropic.com/settings/keys>
2. Sign in, then **Create Key**. Name it anything. Copy it — it is shown once.
3. Make sure the account has credit: **Billing → Add credits**. This is
   separate from a Claude.ai subscription. $5 goes a long way; a typical inbox
   question costs about a cent.

### Google Gemini

1. Go to <https://aistudio.google.com/apikey>
2. **Create API key**, and pick a Google Cloud project (a new one is fine —
   this can be the same project you create in step 2, or a different one).
3. The free tier works immediately but is rate-limited per minute and per day.
   For heavier use, enable billing on that project.

**Do not put the key in a file.** The app asks for it on first run and stores it
in your operating system's credential manager — Windows Credential Manager,
macOS Keychain, or the Linux Secret Service. See
[PRIVACY.md](PRIVACY.md) for exactly where it goes and where it does not.

---

## Step 2 — Create the Google Cloud project

1. Go to <https://console.cloud.google.com/projectcreate>
2. **Project name:** `personal-agent` (anything works)
3. Click **Create**, wait ~10 seconds.
4. **Important:** check the project picker in the blue bar at the top says
   `personal-agent`. If it doesn't, click it and select the project. Every step
   below applies to the *selected* project.

---

## Step 3 — Enable the five APIs

Open each link and click the blue **Enable** button. Each takes a few seconds.

1. Gmail — <https://console.cloud.google.com/apis/library/gmail.googleapis.com>
2. Drive — <https://console.cloud.google.com/apis/library/drive.googleapis.com>
3. Calendar — <https://console.cloud.google.com/apis/library/calendar-json.googleapis.com>
4. People (contacts) — <https://console.cloud.google.com/apis/library/people.googleapis.com>
5. Tasks — <https://console.cloud.google.com/apis/library/tasks.googleapis.com>

If a link opens on the wrong project, switch it in the top bar and reload.

Miss one and the matching tools fail later with a `SERVICE_DISABLED` error that
names the API — come back and enable it then.

---

## Step 4 — Configure the consent screen

1. Go to <https://console.cloud.google.com/auth/overview>
2. Click **Get started**.
3. **App name:** `Workspace Agent` — this is what users see on the sign-in
   screen. **User support email:** pick your address from the dropdown. **Next**.
4. **Audience:** choose **External**. **Next**.
   ("Internal" only exists if you have a Google Workspace organization.)
5. **Contact information:** your email again. **Next**.
6. Tick the agreement box, click **Create**.

---

## Step 5 — Declare the scopes

The app requests seven permissions. Google wants them listed up front.

1. Go to <https://console.cloud.google.com/auth/scopes>
2. Click **Add or remove scopes**.
3. Scroll the panel to **Manually add scopes** and paste all seven at once:

   ```
   https://www.googleapis.com/auth/gmail.modify
   https://www.googleapis.com/auth/gmail.send
   https://www.googleapis.com/auth/calendar
   https://www.googleapis.com/auth/drive
   https://www.googleapis.com/auth/contacts.readonly
   https://www.googleapis.com/auth/contacts.other.readonly
   https://www.googleapis.com/auth/tasks
   ```

4. Click **Add to table**, then **Update** at the bottom of the panel.
5. Click **Save**.

Google will flag some of these as *restricted*. That is expected and does not
block you — see step 6.

---

## Step 6 — Publish the app

1. Go to <https://console.cloud.google.com/auth/audience>
2. Click **Publish app**, then **Confirm**.

**Why:** while the app sits in *Testing*, Google expires the refresh token after
**7 days**, so you would re-sign-in every week. Publishing removes that.

**The trade-off:** the app is unverified, so at sign-in everyone sees a
*"Google hasn't verified this app"* screen. Click **Advanced** →
**Go to Workspace Agent (unsafe)**. That warning never goes away without a paid
security audit, and an unverified published app is capped at **100 users total**.
For a private tool, that is the right trade. Details in
[DISTRIBUTION.md](DISTRIBUTION.md).

*(If you'd rather avoid the warning and accept weekly re-sign-ins, skip this
step and instead add each person's address under **Test users** on the same
page.)*

---

## Step 7 — Create the OAuth client

1. Go to <https://console.cloud.google.com/auth/clients>
2. Click **Create client**.
3. **Application type:** **Desktop app** — this matters. The app signs in over a
   loopback redirect that only Desktop clients allow.
4. **Name:** anything. Click **Create**.
5. On the dialog that appears, click **Download JSON**.
6. Move that file into the project folder:

   ```
   C:\Users\mamma\ai-agent\
   ```

   The download has a long name like
   `client_secret_1234-abcd.apps.googleusercontent.com.json`. You can rename it
   to `client_secret.json` or leave it — the app and the build script both
   accept either. What matters is that it sits in the project folder, not in
   `Downloads`.

That one file is used both when you run from source and when you build the
`.exe` for other people. It is gitignored.

---

## Step 8 — Run it

```powershell
cd C:\Users\mamma\ai-agent
.\run.ps1
```

On first run the terminal asks you to connect an AI provider:

```
Step 1 of 2 — connect an AI provider

  1. OpenAI          https://platform.openai.com/api-keys
  2. Anthropic       https://console.anthropic.com/settings/keys
  3. Google Gemini   https://aistudio.google.com/apikey
```

Pick the one you made a key for and paste it in. It is not echoed as you type,
and it is checked against the provider before anything is saved, so a typo or a
key with no credit is caught here rather than at your first question.

Then it opens your browser for Google sign-in:

1. Pick your Google account.
2. On *"Google hasn't verified this app"* → **Advanced** →
   **Go to Workspace Agent (unsafe)**.
3. Review the permissions, click **Continue**.
4. The browser says you can close the tab. Back in the terminal you'll see
   `Signed in as you@gmail.com` and the chat prompt.

Type a question and press Enter. `/help` lists the commands, `/exit` quits.

The chat window (`.\run.ps1 --web`, or the desktop shortcut) does not ask any of
this in the terminal — it opens regardless and collects the same two things in
the page: the API key on its **API Keys** screen, and the Google sign-in from
the account button at the bottom of the sidebar. Until a key is connected it
will not answer questions, and it says so rather than failing at the first one.

---

## Step 9 — The web tools (optional, but do the first part)

**Browser automation** needs a Chromium of its own — about 150 MB, downloaded
once. `run.ps1` does it when it creates the virtualenv; if you already had one,
run it yourself:

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

Without it, everything else still works and `--check` tells you the browser is
unavailable.

**Web search** needs nothing at all — with no key it uses DuckDuckGo. If you
want better results, put **one** of these in `.env`. These are search keys, not
AI provider keys, and unlike those they are ordinary environment settings:

| Provider | Where | Free tier |
|---|---|---|
| Brave Search | <https://brave.com/search/api/> | 2,000 queries/month |
| Tavily | <https://tavily.com/> | 1,000 credits/month |
| Google Programmable Search | see below | 100 queries/day |

Google Programmable Search takes two values: an API key from
<https://console.cloud.google.com/apis/credentials> (enable the *Custom Search
API* first, the same way as step 3) and an engine id from
<https://programmablesearchengine.google.com/> with **Search the entire web**
turned on. Set both `GOOGLE_SEARCH_API_KEY` and `GOOGLE_SEARCH_ENGINE_ID`.

Whichever you set is used first; DuckDuckGo stays behind it as a fallback, so an
outage or a rate limit costs a retry rather than the answer.

---

## Step 10 — Reminder notifications (optional)

Reminders work the moment you create one, and ring while the app is open. To
have them ring when it is **closed**, let Windows run the check:

```powershell
.\run.ps1 --reminders-install
```

That registers a scheduled task called *Lumen OS Reminders* under your own
account — no administrator rights, nothing machine-wide. It runs a one-second
check every minute, and once at logon so anything that came due while the
machine was off still arrives. The same switch is a button on the Reminders
page.

```powershell
.\run.ps1 --reminders-status      # is it on, when did it last run
.\run.ps1 --reminders-uninstall   # turn it off again
```

No permission prompt is involved: Windows toast notifications need no grant from
the user, though they are suppressed while **Focus assist** / Do not disturb is
on, and Lumen must be allowed under *Settings → System → Notifications*. The
browser page also asks for the web Notification permission the first time you
open Reminders — that one is only for banners while the page is open, and
declining it changes nothing about the Windows notifications.

---

## Verify anytime

```powershell
.\run.ps1 --check
```

All `OK` lines means everything is wired up. Two deeper probes, both safe to run
whenever:

```powershell
.\.venv\Scripts\python.exe scripts\verify_google.py   # every Google scope, read-only
.\.venv\Scripts\python.exe scripts\verify_web.py      # search, and the browser
```

---

## Changing or adding an AI provider

Not part of setup — you can do this whenever:

```powershell
.\run.ps1 --providers
```

or type `/providers` in a chat, or open **API Keys** in the browser UI. From
there you can connect a second and third provider, switch which one is active,
choose a model, test a connection, replace a key, or remove one. Keys stay in
your OS credential manager throughout.

---

## Permissions being granted

| Scope | What it allows |
|---|---|
| `gmail.modify` | Read, label, archive, trash mail; create drafts |
| `gmail.send` | Send mail as you |
| `calendar` | Read and write events on your calendars |
| `drive` | Read, create, and share your Drive files |
| `contacts.readonly` | Look up names, addresses, phone numbers |
| `contacts.other.readonly` | Look up people you've emailed but not saved |
| `tasks` | Read and write Google Tasks |

Notably absent: permanent deletion. `gmail.modify` cannot bypass the trash, so
nothing the agent does to your mail is unrecoverable within 30 days.

To revoke access: <https://myaccount.google.com/permissions> → find the app →
**Remove access**. Then run `/signout` in the app.

---

## Troubleshooting

**`Access blocked: Workspace Agent has not completed the Google verification process`**
The app is still in *Testing* and your address isn't a test user. Either publish
it (step 6) or add yourself under **Test users**.

**`SERVICE_DISABLED` / `... API has not been used in project ...`**
That API isn't enabled. The error names it — enable it via step 3, wait a
minute, retry.

**`insufficient authentication scopes`**
The saved token predates a scope change. Run `/signout` then `/signin`.

**`Playwright is not installed` / `Chromium is not downloaded`**
Step 9. `pip install -r requirements.txt`, then
`.\.venv\Scripts\python.exe -m playwright install chromium`.

**A site keeps asking the agent to log in**
It cannot log in for you, by design — it does not type passwords. Sign in
yourself in the browser window it opened; the profile keeps the session for
next time. If the window is not visible, unset `AGENT_BROWSER_HEADLESS`.

**`no search provider could answer`**
Every backend failed, which usually means no internet or a rate limit on a
keyless DuckDuckGo query. `activity.log` names what each one said.

**Reminders are not notifying me**
Check `.\run.ps1 --reminders-status`. If it says *Not installed*, run
`--reminders-install`. If it says *Ready* but nothing appears, look for Focus
assist being on, or Lumen switched off under *Settings → System →
Notifications*. `activity.log` records every reminder that fired, so you can
tell "it never ran" from "it ran and Windows swallowed the toast".

**`Access is denied` when installing the reminder task**
Something on the machine is blocking per-user scheduled tasks — usually a
managed/work profile. Reminders still work whenever the app is open; there is no
way around the policy from here.

**Sign-in worked, then stopped after about a week**
The app is still in *Testing*. Publish it (step 6).

**`.\run.ps1` → "running scripts is disabled on this system"**
PowerShell's execution policy. Either:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```
(lasts for that window only), or skip the script entirely:
```powershell
.\.venv\Scripts\python.exe -m agent
```

**The browser never opens**
Copy the URL printed in the terminal into a browser by hand. If the redirect
then fails, something is blocking the loopback listener on localhost.
