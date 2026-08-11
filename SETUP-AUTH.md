# Setting up authentication

This is the setup guide for the multi-user backend in `server/`. The desktop app
on its own still runs exactly as it did — this adds a server in front of it so
that many people can each have their own account, their own data, and their own
connected Google account.

Read [AUTH.md](AUTH.md) first if you want to know *how* it works. This document
is the checklist for getting it running.

**`LUMEN_API_URL` is the switch.** Set it and the app signs in to that
deployment; leave it unset and there are no accounts at all — no sign-in screen,
no account items in the menu, and `/api/auth/*` is not served. That is the mode
the packaged desktop build ships in, and it is why step 6 below exports the
variable rather than relying on a default: a developer running the server on
their own machine has to say so.

---

## 1. What you need

| | |
|---|---|
| A Supabase project | Free tier is enough to start. Gives you Postgres, the auth server, and email delivery. |
| A Google Cloud project | For the Gmail/Drive/Calendar integrations. You already have one if the desktop app's Google sign-in works. |
| Python 3.11+ | Same interpreter the app uses. |

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-server.txt
```

---

## 2. Create the Supabase project

1. Go to [supabase.com/dashboard](https://supabase.com/dashboard) and create a
   project. Note the database password it generates — you will not be shown it
   again.
2. **Project Settings → API**, copy:
   - the **Project URL** → `SUPABASE_URL`
   - the **anon / publishable** key → `SUPABASE_ANON_KEY`
   - the **service_role** key → `SUPABASE_SERVICE_ROLE_KEY`
3. **Project Settings → Database → Connection string → URI**, copy it to
   `DATABASE_URL`. Use the **pooler** URI (port 6543) in production and the
   direct one (5432) for running migrations.

### Turn on email confirmation

**Authentication → Sign In / Providers → Email**:

- **Confirm email**: ON. The server is written on the assumption that signup
  returns no session; leaving this off makes signup log people straight in and
  weakens the "verify before connecting a mailbox" rule.
- **Secure email change**: ON. Sends a confirmation to the old address too, so
  somebody who borrows a session cannot quietly move the account to their own
  address.
- **Minimum password length**: 12, to match `server/security/passwords.py`.
  The server checks first and gives a better message, but the floor should
  agree.

### Point the email links at your app

**Authentication → URL Configuration**:

- **Site URL**: your `LUMEN_APP_URL`.
- **Redirect URLs**: add `<LUMEN_APP_URL>/auth/verified`,
  `<LUMEN_APP_URL>/auth/reset`, and `<LUMEN_APP_URL>/auth/callback`.

Supabase refuses any `redirect_to` not on that list, which is what stops a
crafted link from mailing somebody a live token pointed at another site.

### Custom SMTP is not optional, even in development

Supabase's built-in sender is capped at **2 emails per hour**, and the limit is
not adjustable: Authentication → Rate Limits shows "Rate limit for sending
emails" as a *disabled* field until custom SMTP is configured. Two emails an
hour is roughly two signups an hour, which is not enough to test a signup flow,
let alone run one.

The symptom, if you hit it, is a 429 from `/api/auth/signup` with
`Too many attempts. Try again shortly.` That message is this application
correctly translating GoTrue's `over_email_send_rate_limit` — the limit is
Supabase's, not ours, and clearing `public.rate_limits` will not help.

Fix it at **Project Settings → Authentication → SMTP Settings**: point it at
Resend, SES, Postmark or similar. Resend's free tier is 3,000 emails a month
and takes about five minutes. Then verify your sending domain (SPF, DKIM,
DMARC), or the verification emails will go to spam — which looks exactly like
"signup is broken" to a new user.

Once custom SMTP is set, the emails-per-hour field becomes editable; 30 is a
sensible development value.

---

## 3. Run the migrations

The migrations live in `supabase/migrations/` and are ordinary SQL. They apply
in filename order and are safe to re-run.

**With the Supabase CLI** (recommended — it tracks what has been applied):

```powershell
npm install -g supabase          # or scoop install supabase
supabase link --project-ref <your-project-ref>
supabase db push
```

**With psql**, if you would rather not install the CLI:

```powershell
$env:PGPASSWORD = "<your database password>"
foreach ($f in Get-ChildItem supabase/migrations/*.sql | Sort-Object Name) {
    psql "<your DATABASE_URL>" -v ON_ERROR_STOP=1 -f $f.FullName
}
```

**From the dashboard**: SQL Editor, paste each file in order, run.

### Check they applied

```sql
select * from public.rls_gaps();
```

This must return **zero rows**. Any row means a table is reachable by signed-in
users without a policy deciding *which* rows — the exact hole this whole design
exists to prevent. `rls_gaps()` is also asserted by the test suite.

---

## 4. Generate the token encryption key

```powershell
.\.venv\Scripts\python.exe -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
```

Put it in `LUMEN_TOKEN_ENCRYPTION_KEY`. **Back it up somewhere other than the
database.** It encrypts every stored OAuth token; losing it means every user has
to reconnect their integrations. (Nothing else breaks — no account is lost.)

---

## 5. Configure Google OAuth

You need a **Web application** client. This is *not* the Desktop client in
`credentials/` that the standalone app uses — keep both.

1. [console.cloud.google.com](https://console.cloud.google.com) → **APIs &
   Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Web application**.
3. **Authorised redirect URIs** — add exactly:
   ```
   <LUMEN_PUBLIC_URL>/api/integrations/google/callback
   ```
   For local development that is
   `http://127.0.0.1:8000/api/integrations/google/callback`. Google matches this
   character for character, including the scheme and any trailing slash.
4. Copy the client ID and secret into `GOOGLE_OAUTH_CLIENT_ID` and
   `GOOGLE_OAUTH_CLIENT_SECRET`.
5. **APIs & Services → Enabled APIs** — enable Gmail API, Google Calendar API,
   Google Drive API, Google People API, and Google Tasks API.

### The consent screen

Under **OAuth consent screen**, add the scopes listed in
`server/services/permissions.py`. While the app is in **Testing** you must add
each tester's Google address explicitly, and refresh tokens expire after 7 days.

Going to **Production** requires Google's verification, and because `gmail.modify`
and `drive` are restricted scopes that means a **CASA security assessment** —
budget several weeks and a few thousand dollars. Two ways to soften that:

- Ship with only the non-restricted capabilities enabled (Calendar, Tasks,
  Contacts) and add Gmail/Drive after verification.
- Use **Internal** user type if everyone is in one Google Workspace, which skips
  verification entirely.

### Google as a *sign-in* method (optional)

Separate from the above. In Supabase, **Authentication → Sign In / Providers →
Google**, paste a client ID and secret, and add
`<SUPABASE_URL>/auth/v1/callback` to that client's redirect URIs.

Signing in with Google establishes identity only. It does **not** grant the
agent access to Gmail or Drive — that is a second, explicit consent with its own
scopes. Keeping them apart is deliberate: someone who uses Google to log in has
not thereby handed over their mailbox.

### Apple (optional)

Needs an Apple Developer account (£79/year). Create a Services ID and a Sign in
with Apple key, then fill in **Authentication → Sign In / Providers → Apple** in
Supabase and set `APPLE_OAUTH_CLIENT_ID` here. The button appears automatically
once it is configured; until then it is not drawn. Apple also requires that if
you offer any third-party sign-in on iOS, you offer Sign in with Apple too —
irrelevant for a Windows desktop app, relevant the day there is an iOS client.

---

## 6. Run it

```powershell
# Terminal 1 — the API
.\.venv\Scripts\python.exe -m uvicorn server.main:app --reload --port 8000

# Terminal 2 — the desktop app, pointed at it
$env:LUMEN_API_URL = "http://127.0.0.1:8000"
.\run.ps1
```

The server refuses to start if configuration is missing or a tool accepts a
caller-supplied user id. That is intentional — the failure belongs at boot, not
at the first request.

Check it is alive:

```powershell
curl http://127.0.0.1:8000/healthz
```

---

## 7. Test it locally

### The suite

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

93 tests, no infrastructure needed — Supabase and Postgres are faked. Covers the
signup/login/reset matrix, token verification, encryption, redaction, rate
limiting and agent isolation.

### The data-isolation suite

This one needs a real Postgres, because row-level security is the thing under
test and a fake cannot demonstrate it.

```powershell
# A throwaway database
docker run -d --name lumen-test -e POSTGRES_PASSWORD=postgres -p 55432:5432 postgres:16
$env:TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:55432/postgres"
.\.venv\Scripts\python.exe -m pytest tests/test_isolation.py -v
```

Or against `supabase start`, which gives you the real `auth` schema:

```powershell
supabase start
$env:TEST_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
.\.venv\Scripts\python.exe -m pytest tests/test_isolation.py -v
```

It builds the schema from the actual migration files, so it also proves they
apply cleanly to an empty database.

### Testing with two users

The point of the exercise, so do it by hand once as well:

1. Sign up as `a@example.com`, confirm the email, sign in.
2. Create a reminder, send the agent a message, connect Google.
3. Sign out. Sign up as `b@example.com` and do the same.
4. As B, try to reach A's data:

```powershell
# Grab B's token from the login response
$b = (Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/auth/login `
      -ContentType application/json `
      -Body '{"email":"b@example.com","password":"..."}').access_token

# A's conversation id, taken from A's session. Must come back 404.
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/agent/conversations/<A's id>" `
    -Headers @{ Authorization = "Bearer $b" }

# B's own list must contain only B's rows.
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/agent/conversations `
    -Headers @{ Authorization = "Bearer $b" }
```

Then ask B's agent *"read my emails"* and confirm it reaches B's mailbox, or
says Google is not connected — never A's.

### Confirming no token is exposed

```powershell
# Nothing in any response should contain a refresh token.
Invoke-RestMethod -Uri http://127.0.0.1:8000/api/integrations `
    -Headers @{ Authorization = "Bearer $b" } | ConvertTo-Json -Depth 8
```

And in SQL, as a signed-in user rather than as `postgres`, this must raise
*permission denied* rather than return a row:

```sql
set local role authenticated;
select set_config('request.jwt.claims', '{"sub":"<user id>","role":"authenticated"}', true);
select * from public.integration_secrets;
```

---

## 8. Before you go to production

- [ ] `LUMEN_ENV=production` and `LUMEN_REQUIRE_HTTPS=true`. The server will not
      start otherwise, which is the intent.
- [ ] TLS terminated in front of the API, and the proxy run with
      `--proxy-headers` so `X-Forwarded-For` and `X-Forwarded-Proto` are
      trusted. **The proxy must overwrite `X-Forwarded-For`, not append to a
      client-supplied one** — otherwise the rate limiter can be evaded by
      forging the header.
- [ ] Custom SMTP configured and the sending domain verified (SPF/DKIM/DMARC).
- [ ] `DATABASE_URL` pointed at the pooler, not the direct connection.
- [ ] Point-in-time recovery enabled on the Supabase project.
- [ ] `LUMEN_TOKEN_ENCRYPTION_KEY` backed up somewhere other than the database.
- [ ] `select * from public.rls_gaps();` returns zero rows against production.
- [ ] Schedule the housekeeping sweep, once your plan has `pg_cron`:
      ```sql
      select cron.schedule('lumen-sweep', '*/15 * * * *', 'select public.sweep_expired()');
      select cron.schedule('lumen-prune', '0 3 * * *', 'select public.prune_auth_events(90)');
      ```
- [ ] Google OAuth client verified for the scopes you actually ship.
- [ ] Answer the operator checklist at the end of `PRIVACY.md` — retention,
      hosting region, who runs it, who sends the email, backups, legal basis.
      The rest of that document is written; those six are the ones only you can
      answer, and it should not be shown to a user until they are.
- [ ] Consider adding the Have I Been Pwned range check noted at the bottom of
      `server/security/passwords.py`.
- [ ] Consider MFA. Supabase supports TOTP enrolment; nothing in this codebase
      blocks it, but no UI has been built for it.

---

## Troubleshooting

**"Missing required configuration"** — the server lists exactly which variables
are unset. They are all in `.env.example`.

**"token rejected" on every request** — `SUPABASE_URL` does not match the
project that issued the token. The issuer is checked, so a token from another
project fails here by design. Check for a trailing slash.

**Login works, then everything 401s** — the clock. JWT validation allows 30
seconds of drift; a VM with a badly skewed clock will fail everything.

**`invalid_grant` when the agent uses Google** — the user revoked the grant at
Google, or the OAuth client is still in Testing (where refresh tokens expire
after 7 days). The connection is flagged `needs_reauth` and the UI offers a
reconnect.

**Signup returns 429 "Too many attempts"** — almost always Supabase's 2/hour
email cap, not this app's rate limiter. Confirm by calling GoTrue directly:

```powershell
curl -X POST "$env:SUPABASE_URL/auth/v1/signup" `
  -H "apikey: $env:SUPABASE_ANON_KEY" -H "Content-Type: application/json" `
  -d '{"email":"probe@example.com","password":"Correct-Horse-Battery9"}'
```

`{"error_code":"over_email_send_rate_limit"}` means configure custom SMTP.

**Emails never arrive** — you are on Supabase's built-in SMTP and have hit its
hourly cap. Configure your own.
