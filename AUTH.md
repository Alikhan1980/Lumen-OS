# How authentication works

The requirement was that no user's data, credentials or agent can ever reach
another user. This document is the argument that it cannot, written so it can be
checked rather than trusted.

[SETUP-AUTH.md](SETUP-AUTH.md) is the practical guide. This one is the design.

---

## The shape of it

```
  Desktop app                    API server                    Supabase
  ───────────                    ──────────                    ────────
  chat page                                                    GoTrue
     │  (no token, ever)                                       ├ password hashing
     ▼                                                         ├ verification mail
  loopback server ──── Bearer <access token> ────► FastAPI ────┤ reset tokens
     │                                                │        └ social sign-in
     ├ refresh token in                               │
     │ OS credential store                            ▼        Postgres
     │                                        verify signature ├ RLS on every table
     └ access token in memory                        │        └ encrypted secrets
                                                      ▼
                                            SET LOCAL role authenticated
                                            SET LOCAL request.jwt.claims
                                                      │
                                                      ▼
                                            every query filtered by auth.uid()
```

Nothing between the desktop app and Postgres ever decides *whose* data to return
by reading a value from a request body. There is one source of identity — the
signature on the access token — and it is checked on every request.

---

## The five guarantees

### 1. A user id can only come from a verified signature

`server/security/tokens.py` is the only place a `Principal` is created, and the
only way to get one is a passing signature check: algorithm from an allowlist
(so `alg: none` is refused), issuer matched against the project, audience
matched, expiry enforced, and the role required to be `authenticated` (so a
service-role key is not a user).

`server/deps.py` offers routes exactly three shapes — `CurrentUser`,
`VerifiedUser`, `ServiceDb` — and there is no dependency that yields a user id
from anything else. Whether an endpoint is protected is visible in its
signature.

### 2. The database enforces isolation, not the application code

Every user-owned table carries `user_id uuid references auth.users(id) on delete
cascade`, has RLS enabled **and forced**, and has a policy of
`user_id = auth.uid()`.

`server/db.py` runs each request in a transaction that drops to the
`authenticated` Postgres role and installs the verified claims as
`request.jwt.claims`. From there:

- a route that forgets `where user_id = ...` still returns only the caller's rows;
- a guessed primary key matches nothing and returns 404, not 403 — 403 would
  confirm the row exists;
- `SET LOCAL` is transaction-scoped, so a pooled connection cannot carry one
  user's identity into the next request.

Every write policy has **both** `USING` and `WITH CHECK`. Without the second, a
user could update one of their own rows and set `user_id` to somebody else —
giving a row away rather than reading one, but a breach either way.

The migration starts by revoking all privileges from `anon` and `authenticated`
and only then grants back what each table needs. A table added later and
forgotten is invisible, not public. `public.rls_gaps()` is the tripwire, and the
test suite asserts it returns nothing.

### 3. OAuth tokens cannot leak, because they are not reachable

`integration_secrets` grants the `authenticated` role **nothing**. A user cannot
read their own refresh token. That is stronger than "the API strips the field":
no route, serialiser, export or logging bug can return one, because the row is
not in that connection's universe at all.

The tokens are also AES-256-GCM encrypted before they are written
(`server/security/crypto.py`), with the connection id and column name bound in
as additional authenticated data — so a ciphertext moved between rows fails to
decrypt. A database dump on its own yields nothing usable; the key is in the
server's environment.

### 4. The agent is bound to a user it cannot argue with

`agent/context.py` holds a `ContextVar` set from the verified user id after
authentication and read by `agent/tools/google_auth.py` when it builds a Google
client. Three properties matter:

- **Task-local.** Concurrent turns cannot see each other's binding. The old
  module-level `functools.cache` on `service()` would have handed one user's
  memoised Gmail client to whoever asked next; it is now keyed by identity, and
  on the server path no client is cached at all.
- **Not an argument.** Tools receive what the model produced. The context is not
  in that set — a tool *asks for* it, it is never passed in. So no string the
  model can emit changes whose credentials are used.
- **Absent by default.** With nothing bound, the desktop app behaves exactly as
  it did before.

`assert_no_identity_parameters()` runs at startup and refuses to boot if any
registered tool accepts `user_id`, `account_id`, `on_behalf_of` or similar. The
test suite runs the same check over the real registry, and a second test proves
the check itself is not vacuous.

`get_credentials()` — the desktop path that reads `token.json` and can open a
browser for consent — now raises if a user is bound, so a stray call on the
server cannot hand out the host's own Google account or try to run a consent
flow on a headless box.

### 5. The tool list is the permission model

`server/services/permissions.py` maps capabilities ("Read and organise your
email") to the scopes they need and the tools they enable. The agent's tool list
is filtered to what the user's grants actually cover, so a user who never
granted Gmail send does not get `gmail_send_email` refused — the model never
learns it exists.

Capabilities are computed from the scopes Google *granted*, not the ones that
were requested, because a user can untick items on the consent screen.

---

## Decisions worth knowing about

**Supabase Auth rather than our own.** Password hashing, verification mail,
single-use expiring reset tokens and social sign-in are all things with a known
right answer and a long history of subtle wrong ones. None of them are
implemented here. The reset flow in particular is safe by construction: we never
generate, store or validate a reset token, so "reset tokens must never appear in
logs" is not a rule anybody has to remember.

**The API proxies GoTrue instead of the client calling it directly.** One door
to rate limit and audit; the anon key stays off the client; and our own error
vocabulary, which deliberately refuses to distinguish cases GoTrue distinguishes
— "no such user" and "wrong password" get one answer.

**Enumeration is treated as a real vulnerability.** Signup returns the same 202
whether or not the address is taken. Forgot-password and resend-verification say
the same thing either way. Login gives one message for a wrong password and an
unknown address. The tests assert byte-identical responses rather than merely
similar ones.

The one exception: a *correct* password against an unverified account says so.
That is only reachable by somebody who already holds the credentials, and
without it a user who never clicked the link is told their correct password is
wrong.

**Rate limits count on two axes.** Per IP catches one host spraying many
accounts; per account catches many hosts against one. A limiter with only the
first is defeated by a botnet. Counters live in Postgres, not process memory —
an in-memory limiter stops limiting the moment there are two instances. The
account axis is keyed by an HMAC of the address, so the table is not a list of
everyone who has ever tried to sign in.

**Split token storage.** Access token in the response body, short-lived. Refresh
token in an httpOnly, SameSite=Strict, path-scoped cookie for browsers, or the
OS credential store for the desktop app. An XSS then costs the life of one
access token rather than a durable credential. The desktop page holds neither —
it talks to loopback, and loopback attaches the header.

**Redaction is enforced twice.** A filter sweeps every log record for JWTs,
Google refresh tokens, API keys, bearer headers and `password=`-style
assignments — which catches the case nobody plans for, an exception whose
message contains the request body. And `safe()` renders structured fields
through an *allowlist*, so a newly named field is invisible until somebody adds
it deliberately.

**Deletion is a cascade, not a list.** Every table references `auth.users` with
`on delete cascade`, so removing the auth user removes everything in one
transaction. A hand-written list of tables to clear is a list somebody forgets
to extend, and the failure mode is orphaned personal data the user was told had
been erased. Integrations are revoked at Google *first*, because deleting our
copy of a token is not the same as revoking the grant.

---

## What is deliberately not here

- **MFA.** Supabase supports TOTP enrolment and nothing in this design blocks
  it, but no UI has been built.
- **A breach-corpus password check.** The hook and the method (k-anonymity
  against HIBP's range API) are documented at the bottom of
  `server/security/passwords.py`; it is off by default because it puts a third
  party in the signup path.
- **Session listing and per-device revocation.** "Sign out of all devices" works
  through GoTrue's global scope, but there is no screen listing active sessions.
- **Organisations or sharing.** Every policy is `user_id = auth.uid()`. Adding
  teams means a membership table and rewriting every policy — worth knowing
  before somebody promises it.

## What this changes about the product

`PRIVACY.md` said, accurately for the desktop-only app: *"There is no account to
create with us, no server of ours in the path, and nothing to sign up for."*
Running the backend makes that false, so it has been rewritten — the claim is
now conditional on `LUMEN_API_URL`, which is what actually decides it, and there
is a section describing what the server stores and who can reach it, taken from
the migrations.

**One part of it is deliberately unfinished**, and cannot be finished from the
code: how long a deployment keeps things, where it is hosted, who operates it,
and who sends its email are facts about *running* the service, not about
building it. They are a checklist at the end of that document and must be
answered before it is shown to anybody.
