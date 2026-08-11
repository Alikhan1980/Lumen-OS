# Lumen OS — what to add next

A sequenced roadmap for the Google Workspace Agent, ordered by value per unit of
risk rather than by how interesting each item is.

## Where the project stands

7,592 lines of first-party Python, 31 tools across six Google surfaces, a
hand-written browser UI, an optional metering proxy, 92 offline checks, and a
PyInstaller build that ships to about ten people. The code is unusually
disciplined — zero TODO markers, comments that explain *why*, a system prompt
that is a real prompt-engineering artifact, and three cache-stability decisions
(sorted tool list, byte-stable system prompt, per-turn context injected as a
message) that most people building on the Messages API get wrong.

So the honest answer to "what should I add" is not *more tools*. The gaps are in
four places: the project has no version control, the agent forgets everything
between runs, it is blind to anything outside your Google account, and it runs
its tools one at a time.

---

## Phase 0 — Foundation

### ⚠ Do this first — it takes thirty seconds

**The project is not under version control.** `git rev-parse` returns *fatal:
not a repository*. There is no `.git` anywhere. Meanwhile `.gitignore` already
exists and is correctly written — secrets, credentials, build output, workspace
and the proxy ledger are all excluded.

```bash
git init
git add -A
git commit -m "Initial commit — Lumen OS 1.2.0"
```

Everything below is a change to a codebase with no undo. Do not start any of it
first.

| Item | Effort | Why |
|---|---|---|
| `git init` | 30 sec | See above. `.gitignore` is already correct. |
| `pyproject.toml` + pytest wrapper | 1 hr | Keep `scripts/selftest.py` — it is good, and it stubs Anthropic and Google so it costs nothing. Add a thin `tests/test_selftest.py` that runs its 16 functions, so `pytest` works and CI has something standard to call. |
| GitHub Actions | 30 min | `ruff check` plus the self-test on every push. `ruff.toml` already enables the `S` (bandit) rules — worth having enforced rather than remembered. |
| **A cache-prefix regression test** | 15 min | Ten lines, and the whole defence for every item below that touches `tools` or `system`. Send two turns against the stubbed client and assert `json.dumps(a["tools"]) == json.dumps(b["tools"])`, that `system[0]["text"]` is byte-identical, and that `cache_control` sits on the first block only. A prefix that silently varies produces `cache_read_input_tokens: 0` forever while cache *writes* bill at 1.25× — no error, just a doubled bill. |
| Resolve the name | 1 hr | The UI, `ui.py:192`, both `.bat` titles and the self-test say **Lumen OS**. README, SETUP, DISTRIBUTION, `proxy/app.py:92` and the exe say **Google Workspace Agent**. `web.py:2274` tells the user to double-click "Workspace Agent" on their desktop. Pick one before ten people learn two names for it. |

---

## Phase 1 — Reliability and speed

The largest felt improvement per line changed. Nothing here needs a new Google
scope, so it ships without asking anyone to re-consent.

### 1. Parallel tool execution — with a prerequisite

`core.py:295` runs tool calls in a plain `for` loop. Google API calls are
I/O-bound at roughly 200–800 ms each, and your own system prompt tells the model
to batch reads ("*request them together in one turn rather than one at a
time*"), so multi-tool rounds are the common case. Reading five messages costs
five round trips in series.

> **You cannot just wrap this in a ThreadPoolExecutor.** `google_auth.py:149` is:
>
> ```python
> @functools.cache
> def service(api: str, version: str):
>     return build(api, version, credentials=get_credentials(), cache_discovery=False)
> ```
>
> That returns *one shared* `googleapiclient` Resource per API, used by all five
> Google modules (`gmail.py:19`, `calendar.py:14`, `contacts.py:13`,
> `drive.py:30`, `tasks.py:13`). It holds one `httplib2.Http`, which pools live
> sockets in a plain dict (`httplib2/__init__.py:1283`, `:1592`) and hands two
> threads the same `HTTPSConnection`. `googleapiclient` has no thread guard at
> all.
>
> Failure modes in ascending nastiness: `ResponseNotReady` / `CannotSendRequest`
> (loud), `BadStatusLine` (loud), and **thread A receiving thread B's response
> body** (silent). The last one means the agent shows the user one email's
> contents labelled as another's — in an app whose entire premise is acting on
> real data.

Order of work:

1. **Make the service cache per-thread — and version it.** Swap
   `@functools.cache` for a `threading.local()` cache keyed by a module-level
   generation counter.

   The counter is not polish. `reset_service_cache()` is called on account
   switch (`web.py:250`, `__main__.py:75`) and sign-out
   (`google_auth.py:186`). With `threading.local()`, the main thread can only
   clear *its own* copy — pool workers would keep serving the **previous Google
   account**. `web.py:257` already carries the comment *"A different person is
   now driving; their chat must not start with the previous account's mail and
   files already in context"*; a naive per-thread cache silently breaks exactly
   that guarantee. Bump a generation on reset and check it on every `service()`
   read.

   Cost is low: `build()` makes no network call — static discovery is bundled
   (`gmail.v1.json`, `drive.v3.json`) and shipped via `collect_data_files` in
   `WorkspaceAgent.spec:6`. It's a JSON parse, ~50–150 ms, once per worker
   thread per API.

2. **Lock credential refresh and make the token write atomic.**
   `get_credentials` returns early when `creds.valid`, so the
   `TOKEN_PATH.write_text` at line 115 only runs on the refresh path or a fresh
   login — not on every call. But that path *will* be hit concurrently at token
   expiry, `write_text` truncates before writing, and
   `google.auth.credentials.Credentials` has no internal refresh lock. A
   truncated `token.json` means the user must re-authenticate. Wrap the refresh
   in an `RLock` and write via `.tmp` + `os.replace` (atomic on Windows for
   same-volume renames).

3. **Run the confirmation gate serially, on the main thread, before dispatch.**
   `_run_tool` currently interleaves `spec.confirm` with execution. A background
   thread cannot drive the terminal prompt or the web UI's approval SSE
   round-trip, and four threads asking at once would interleave four prompts.
   Split it: collect approvals for all gated calls first, then dispatch only the
   approved ones.

4. **Preserve result order.** Write into a pre-sized list at each call's own
   index — never append from `as_completed`. The API matches by id, but
   positional order keeps the transcript readable and keeps
   `selftest.py:243` valid.

5. **Emit callbacks from the dispatching thread only.** `TerminalCallbacks`
   (`ui.py:36`) holds one rich `Status` plus `_wrote_text` / `_thinking_open`,
   all mutated by `on_tool_start` / `on_tool_end`. Four threads calling it
   leaves the spinner running after the last tool, or interleaves prints
   mid-line. Don't make it thread-safe — have the worker return results and let
   the main thread emit. Also add a `call_id` to both events: `WebCallbacks`
   (`web.py:158-170`) keys by tool *name*, so two concurrent
   `gmail_read_message` calls would update the wrong browser row.

6. **Keep writes serial.** Everything `confirm=True`, plus `file_write` —
   `localfiles._resolve` calls `load_config()` on every invocation, which does
   `mkdir(parents=True, exist_ok=True)` twice, and concurrent mkdir on Windows
   can raise.

7. **Cap concurrency at 4, not 8.** `gmail_search(max_results=50)` already fires
   51 sequential HTTP calls *inside one tool* (`gmail.py:224-238`). Four
   concurrent tools each doing that approaches Gmail's 250 quota-units/sec
   ceiling; eight draws `userRateLimitExceeded`, which surfaces to the model as
   a raw exception string. Four turns five 400 ms reads from 2.0 s into ~0.8 s —
   that is the whole win.

### 2. Retry — half a one-liner, half a real fix

`core.py:254` reads `except anthropic.APIError: self._rollback_turn(); raise`,
which looks like a turn dies on the first 429. It does not: the SDK already
retries 408, 409, 429 and 5xx with backoff, **twice by default**, on both the
direct and proxy paths. So this only fires after three attempts.

**But the SDK cannot retry the case that actually bites.** Once
`messages.stream()` has returned headers, an `overloaded_error` arriving as an
SSE event surfaces as an exception from the iterator — the stream is already in
your hands, and the SDK has no way to replay it. On round 12 of 25, after 11
successful Gmail calls, `_rollback_turn` then throws all of it away.

Two parts:

**Raise `max_retries`** in both branches of `_build_client` — 4 rather than the
default 2. Not higher: the SDK retries timeouts too, so worst-case wall clock is
`timeout × (attempts + 1)`, and with `PROXY_TIMEOUT = 600s` five attempts is an
hour.

**Add a round-level retry around the stream.** Extract the `with
self.client.messages.stream(...)` block from `send()` into a `_stream_round()`
that catches `APIStatusError` / `APIConnectionError`, retries on
`{408, 409, 429, 500, 502, 503, 529}` with exponential backoff, and honours the
`retry-after` header when present.

This is safe for one specific reason worth writing into the code as a comment:
`self.messages.append({"role": "assistant", ...})` happens *after*
`get_final_message()` returns (`core.py:273`), so a failed round leaves history
untouched and re-sending identical `_request_params()` is genuinely idempotent.
If anything ever starts appending earlier, this retry silently becomes a
duplicate-turn bug.

One honesty detail: if the stream died after 200 tokens, the user already saw
them. Add an `on_retry()` callback so the terminal can print a dim *"(restarting
the response)"* and the browser can clear the in-progress bubble.

Also worth fixing while here: the `RateLimitError` handler in `_chat`
(`__main__.py:188`) says "wait a moment and retry" — after this change, reaching
it means retries were already exhausted.

### 3. Audit log

This agent sends mail on behalf of ten people. Append every `confirm=True`
action to `DATA_DIR/audit.jsonl`: timestamp, tool, arguments, approved/denied,
short result summary.

**Put the hook in the gate, not the handler and not the callbacks.** `--yes` /
`AGENT_AUTO_APPROVE` bypasses `confirm()` entirely (`core.py:201`), so hooking
the callbacks would leave the auto-approved sends — the highest-risk ones —
unlogged. The gate sees all three paths: approved, auto-approved, denied.

Write two lines correlated by id: the decision, then the outcome. One-phase
logging means a crash between approval and completion leaves no record that a
send was approved, which is exactly the case the log exists for.

**Redact with an allowlist, not a denylist.** A denylist of key names ages badly
— the next tool adds a field nobody anticipated. Keep the accountability fields
verbatim (`to`, `cc`, `subject`, `*_id`, `path`, labels), render everything else
as `[N chars]`. So the log shows that mail went to `priya@acme.com` with subject
"Re: Q3 numbers" and a 412-character body, without archiving the body. Never log
tool *results* verbatim — that is where mailbox contents live; log the one-line
summary the callbacks already compute.

> **Write to `DATA_DIR`, never `workspace/` — and the same goes for transcripts
> in item 7.** `workspace/` is reachable by `file_read`, so an audit log or
> transcript stored there could be read back by a prompt-injected turn and
> emailed out via `gmail_send_email`. **`workspace/` is model-reachable;
> `DATA_DIR` is not.** That distinction is the whole containment story — keep it
> intact.

---

## Phase 2 — Make the agent smarter

The theme: the agent starts every session as a stranger, and can only see inside
one Google account.

### 4. Web search and fetch — the cheapest capability jump here

Anthropic hosts these server-side, so there is no client-side execution to
write:

```python
{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5}
```

Four things to know before wiring it in:

- **Use the `_20260209` versions, not `_20250305`.** They carry built-in dynamic
  filtering, and both `claude-sonnet-5` and `claude-opus-5` support them.
  Because filtering runs code under the hood, do *not* separately declare a
  `code_execution` tool — a second execution environment confuses the model.
- **Your loop already handles the hard part.** Server tools emit
  `server_tool_use` and `web_search_tool_result` blocks, not `tool_use` — so
  `core.py:293`'s filter naturally excludes them and `_run_tool` will never try
  to dispatch them locally. And `pause_turn`, which is exactly the server-tool
  iteration-cap signal, is already handled at `core.py:279`.
- **Errors arrive as HTTP 200.** A failed search returns a result block whose
  `content` is an error object rather than a list. Branch on that before
  indexing, or the UI throws on a rate-limited search.
- **Keep the tool list deterministic.** Append the two server-tool dicts at a
  fixed position in `registry.api_definitions()`. Adding them is a one-time
  cache-prefix invalidation; making their position vary would be a permanent
  one.

Unlocks: "look up this company before I reply", "what's the address in this
invite", "is this vendor's outage still ongoing". Billed separately at roughly
$10 per thousand searches, so `max_uses` is worth setting.

### 5. Cross-session memory — use the memory tool, don't hand-roll it

`Agent.messages` is a plain in-RAM list and `reset()` clears it. Nothing
survives a restart, so the agent relearns who your manager is every morning.

The instinct is to write a `memory.md` and inject it into the prompt — resist
it, because that fights your own cache design. Use the client-side memory tool
instead:

```python
{"type": "memory_20250818", "name": "memory"}
```

You have `anthropic` 0.120.2, which ships `BetaAbstractMemoryTool` in
`anthropic/lib/tools/_beta_builtin_memory_tool.py`. Subclass it and implement
`view`, `create`, `str_replace`, `insert`, `delete`, `rename`, `clear`. Your
`localfiles.py` sandbox is very nearly the backend already — the same
`resolve()`-and-check-against-root guard at `localfiles.py:19-27`, pointed at
`DATA_DIR/memories/` instead of the workspace.

Why this shape:

- Memory files load *on demand*, as tool results, so a growing memory does not
  inflate every turn.
- Nothing touches the system prompt, so the cached prefix stays byte-stable.
- The agent decides what is worth remembering — "Sarah is my manager", "Project
  Falcon is the Q3 launch", "never send without asking me first".

One rule for the system prompt: never write credentials or tokens into memory.
Memories replay verbatim into every future session.

### 6. Context management — two server-side betas, not a hand-rolled compactor

`MAX_TOOL_RESULT_CHARS` is 60,000. Three large Drive reads and the window is
under real pressure, with nothing to relieve it.

| Feature | Beta header | What it does |
|---|---|---|
| **Context editing** | `context-management-2025-06-27` | `{"type": "clear_tool_uses_20250919"}` — clears *old tool results*. This is the one that matches your problem: your 60 KB Drive dumps fill the window and are dead weight three turns later. |
| **Compaction** | `compact-2026-01-12` | `{"type": "compact_20260112"}` — summarizes earlier history server-side near the trigger threshold. The safety net for genuinely long sessions. |

Both move you to `client.beta.messages.stream`. Compaction has one hard
requirement you already satisfy: you must append the full `response.content`
back to messages, not just the text — `core.py:273` already does exactly that,
so compaction blocks are preserved. Start with context editing; smaller change,
and it addresses the actual failure mode.

> **Check the proxy before shipping this.** `proxy/app.py` is a Messages API
> passthrough that validates the model and clamps `max_tokens` — verify it
> forwards the `anthropic-beta` header, or proxy users silently get no context
> management while direct-key users do. If forwarding turns out to be awkward,
> a hand-rolled compactor (~120 lines: group history on real user turns, summarize
> everything but the last four with a cheap `claude-haiku-4-5` call, splice in a
> user/assistant summary pair) is fully testable offline and avoids the beta
> path entirely. Either way, **only ever compact at the top of `send()`**, never
> mid-turn — at that point history always ends on a completed assistant turn, so
> the `tool_use`/`tool_result` pairing problem doesn't exist rather than needing
> to be solved.

If you do build the trigger yourself, note that `Usage` (`core.py:59`)
*accumulates across turns* — after five turns `input_tokens` is the sum of five
prompts, not the current prompt size. Use the last message's usage, and sum
`input_tokens + cache_read_input_tokens + cache_creation_input_tokens`;
`input_tokens` alone is only the uncached remainder.

> **Related cache hazard.** A cache breakpoint walks back at most **20 content
> blocks** to find a prior entry. With `MAX_TOOL_ROUNDS = 25` and parallel tool
> calls landing several blocks per round, a single busy turn can exceed 20
> blocks — and the next request's breakpoint silently misses. Verify with
> `usage.cache_read_input_tokens` on a long tool-heavy turn; if it drops to zero
> mid-conversation, that is why.

### 7. Conversation persistence and resume

Save transcripts to `DATA_DIR/history/<session-id>.json`. Serialization needs
care: `message.content` holds SDK objects, not plain dicts — use
`message.to_dict()` rather than `json.dumps(..., default=str)`, which would
silently produce unreplayable history.

The UI half is already half-built: `web.py:1241` has a sidebar search box with a
`⌘S` hint and no `getElementById` wiring behind it. Session history is the
obvious thing to put there. On the CLI, a `/resume` alongside `/clear`.

---

## Phase 3 — Fill the Google holes

> **Batch every scope change into one release.** Items 10–11 need new OAuth
> scopes, and adding a scope forces all ten users to sign in again. Ship them
> together, once, with a note.

| # | Tool | New scope? | Note |
|---|---|---|---|
| 8 | **`gmail_download_attachment`** | No | **The highest-value single tool here.** `gmail.py:73` already *lists* attachment metadata — the agent can see a PDF is attached and cannot open it. Fetch via `users().messages().attachments().get()`, write into `workspace/attachments/` (which `web.py:101` already manages), return the path so `file_read` picks it up. |
| 9 | Attachments on send/draft | No | `_build_message` (`gmail.py:~300`) builds a flat message. Make it multipart and accept workspace-relative paths. "Reply to Sarah with the contract attached" currently cannot be done. |
| 10 | `sheets_read_range`, `sheets_append_rows`, `sheets_update_range` | `auth/spreadsheets` | Today `drive_read_file` renders a Sheet to CSV but nothing can write one. "Log this expense", "add these leads to the tracker" both fail. Gate the writes with `confirm=True`. |
| 11 | `docs_append`, `docs_replace` | `auth/documents` | `drive_create_file` can make a Doc; nothing can edit one. Lower value than Sheets for inbox work — include it only because it rides the same re-consent. |
| 12 | Meet link on event create | No | One parameter: `conferenceDataVersion=1` plus a `createRequest` in the body. High frequency, near-zero effort. |

---

## Phase 4 — Automation

### 13. Scheduled runs

The feature that changes what the product *is*: an 8 a.m. briefing on what came
in overnight, a Friday digest, a watch-and-notify on a thread you care about.

For a PyInstaller exe going to non-technical Windows users, **Windows Task
Scheduler is the right mechanism** — no daemon to install, supervise or explain,
and it survives reboots for free. Register the task from the app (`schtasks
/create`, or the `win32com` Task Scheduler API) pointing at
`WorkspaceAgent.exe --run-schedule morning`.

Design points that matter more than the plumbing:

- **A headless run must not silently auto-approve.** Do not reuse `--yes` — and
  make `--yes` and `--run-task` mutually exclusive in `main()`, or someone will
  put both in the scheduled command line and never think about it again. Add a
  `HeadlessCallbacks` whose `confirm` denies and *records* what it skipped, with
  a refusal message that says nobody is watching, so a scheduled briefing
  prepares drafts instead of sending. The base `Callbacks.confirm` at
  `core.py:92` already denies — that instinct is already right; the headless
  class only narrows it via an explicit per-task allow list.
- **Headless sign-in must not open a browser.** `get_credentials` falls through
  to `_run_flow()` when the refresh token is dead — which at 8 a.m. means an
  ambush consent window on someone's laptop. Add an `interactive: bool = True`
  parameter; the scheduled path passes `False` and raises instead. Three lines,
  and it prevents the worst first impression this feature could make.
- **Suppress the console flash.** `WorkspaceAgent.spec:39` sets `console=True`,
  so a scheduled run pops a console window daily on ten machines. Route
  `--run-task` output to `DATA_DIR/tasks/<id>.log`.
- **Delivery.** Write the briefing to `workspace/briefings/`, and optionally have
  the agent mail it to you — which reuses `gmail_send_email` and therefore needs
  a narrow, explicit allowance rather than a blanket one. Send it from the
  runner with the recipient forced to `cached_account_email()`, not from
  whatever address the model produced. `os.startfile(path)` is a more reliable
  "it's open when I sit down" than a Windows toast, which is silently suppressed
  on some Windows 11 configurations.
- **Ship `--schedule-remove` before `--schedule-add`.** Task Scheduler entries
  live outside the project directory; `git checkout` will not undo them.

---

## Phase 5 — Ship quality, for the ten

| Item | Why it matters when you are handing this out |
|---|---|
| **Wire or remove the seven dead controls** | Three topbar icon buttons (`web.py:1289-1291`) and the "Search thread" / "Create folder" pills (`web.py:1297-1298`) are styled to look clickable and do nothing — the CSS at `web.py:782, 861-878` just neutralizes the hover. You know they are placeholders; a recipient discovers a broken app. The sidebar search box is the natural home for session history from item 7. |
| **Drive / Tasks / Contacts views** | Only three of six tool groups have a browser view (`VIEWS`, `web.py:1962-1966`). The other three being chat-only is fine — but the empty chrome above implies otherwise. |
| **Single source of truth for the build** | `WorkspaceAgent.spec` is gitignored (`*.spec`) yet sits in the tree, duplicating the flags `build.ps1` passes to PyInstaller. Pick one; if you keep the spec, un-ignore it now that you have a repo. |
| **Cost visibility in the browser UI** | `/cost` is terminal-only, and most of your ten will never open a terminal. The proxy already keeps a per-user SQLite ledger with monthly caps — surface the same number in the web header. |

---

## Two things worth *not* doing

- **Don't add MCP support yet.** It is the fashionable answer and would let users
  plug in Slack or Notion, but your users are ten people who wanted their Google
  account to answer questions. It adds a configuration surface and a trust
  boundary for a capability nobody has asked for.
- **Don't rewrite `web.py`.** 2,280 lines with inlined HTML/CSS/JS looks like it
  wants a framework. It has zero build step, zero dependencies, and ships inside
  a 42 MB exe that already works. The dead controls are worth fixing; the
  architecture is not.

---

## Suggested order

1. **`git init`**, then the cache-prefix test, then pyproject + CI. *Half a day, and everything after it becomes reversible.*
2. **Retry** (item 2). *~40 lines. Fixes a failure users actually hit, with no scope, cache, or schema change.*
3. **Audit log** (item 3). *Append-only side effect, no behaviour change — and it makes scheduled runs defensible later.*
4. **`gmail_download_attachment`** (item 8). *One tool, no scope change, immediately noticeable.*
5. **Parallel tools** (item 1) — as two commits: `google_auth.py` and its per-thread test first, dispatcher second. *The prerequisite is the real work.*
6. **Web search and fetch** (item 4). *Two dicts plus UI rendering — after checking the proxy's server-tool metering.*
7. **Persistence** (item 7), then **memory** (item 5). *Memory needs persistence's account-scoping thinking.*
8. **Context editing** (item 6). *After persistence, so transcripts are saved before history is condensed.*
9. The scope-change release: Sheets, Docs, attachments-on-send, Meet.
10. Scheduled briefings, then Phase 5 polish before the next handout.

**Do not start items 1, 6, or 13 before `git init`.** Parallel dispatch can
produce silently wrong tool results that no hand-revert recovers; compaction
mutates live history in place; and scheduled runs write to the Windows Task
Scheduler, which is state outside the project directory. The audit log, memory,
and persistence are safe to build first — each is a new module plus two or three
call sites, undoable by deleting a file.

One thing already handled: `client_secret.json` is in the project root but
`.gitignore` already excludes it, along with `credentials/`, `workspace/`,
`dist/` and the proxy ledger. Nothing to fix there before the first commit.
