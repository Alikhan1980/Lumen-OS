"""The sign-in, sign-up and onboarding screens, as fragments for the chat page.

Kept out of `agent/web.py` because that file is already long, and injected into
it through three placeholders so the page stays a single document with a single
stylesheet. The alternative -- a second page on a different route -- would mean
a second copy of the design system and a full navigation between "signed out"
and "signed in", which is exactly the kind of seam that makes an auth flow feel
bolted on.

Everything below reuses the tokens already defined in the page: `--bg`,
`--panel`, `--line`, `--ink`, `--muted`, `--indigo`, `--dark`, `--err`. No new
colours, no new fonts, no new radii. The card is the same shape as the existing
provider-setup card, deliberately, so the first screen a new user sees and the
first screen an existing user sees look like the same application.

**No token ever reaches this JavaScript.** Every call goes to the loopback
server, which attaches the Authorization header from the OS credential store.
The page's own auth state is a boolean and a display name.
"""

from __future__ import annotations

# --------------------------------------------------------------------- styles

CSS = """
  /* ------------------------------------------------------------------ auth */

  /* Sits above the provider gate: signing in comes before choosing a model.
     Same card geometry as .setup-card so the two read as one flow. */
  #auth {
    position: fixed; inset: 0; background: var(--bg); z-index: 70;
    overflow-y: auto; padding: 40px 24px;
  }
  #auth[hidden] { display: none; }
  .auth-card {
    max-width: 420px; margin: 0 auto; background: var(--panel);
    border: 1px solid var(--line); border-radius: 20px; padding: 32px;
    box-shadow: 0 20px 60px rgba(0,0,0,.07);
  }
  .auth-brand {
    display: flex; align-items: center; gap: 9px; justify-content: center;
    margin-bottom: 22px;
  }
  .auth-brand .mark {
    width: 27px; height: 27px; border-radius: 8px; background: var(--indigo);
    display: grid; place-items: center; flex: none;
  }
  .auth-brand .mark svg { width: 15px; height: 15px; }
  .auth-brand .wordmark { font-size: 20px; font-weight: 700; letter-spacing: -0.02em; }

  .auth-card h1 {
    margin: 0 0 6px; font-size: 21px; font-weight: 700; letter-spacing: -0.02em;
    text-align: center;
  }
  .auth-card .lede {
    margin: 0 0 22px; color: var(--muted); font-size: 13.5px; text-align: center;
  }

  .auth-view[hidden] { display: none; }

  .field { margin-bottom: 13px; }
  .field label {
    display: block; font-size: 12.5px; color: var(--muted); margin-bottom: 6px;
  }
  .field input {
    width: 100%; padding: 11px 13px; border: 1px solid var(--line);
    border-radius: 11px; font: inherit; font-size: 14px; background: #fff;
    color: var(--ink);
  }
  .field input:focus { outline: 2px solid var(--indigo); outline-offset: -1px; }
  .field input[aria-invalid="true"] { border-color: var(--err); }
  /* The password field and its reveal button share one rounded box. */
  .field .peek-wrap { position: relative; }
  .field .peek-wrap input { padding-right: 62px; }
  .field .peek {
    position: absolute; right: 5px; top: 50%; transform: translateY(-50%);
    border: 0; background: none; padding: 6px 8px; border-radius: 8px;
    font: inherit; font-size: 12.5px; color: var(--muted); cursor: pointer;
  }
  .field .peek:hover { color: var(--ink); background: #f4f4f4; }
  .field .hint { margin-top: 6px; font-size: 12px; color: var(--faint); }

  .auth-row {
    display: flex; align-items: center; justify-content: space-between;
    gap: 12px; margin: 4px 0 16px;
  }
  .auth-row label {
    display: flex; align-items: center; gap: 7px; font-size: 13px;
    color: var(--muted); cursor: pointer;
  }
  .auth-row input[type="checkbox"] { width: 15px; height: 15px; }

  .auth-go {
    width: 100%; padding: 12px; border: 0; border-radius: 11px;
    background: var(--dark); color: #fff; font: inherit; font-weight: 600;
    font-size: 14px; cursor: pointer;
  }
  .auth-go[disabled] { opacity: .55; cursor: default; }

  .auth-alt {
    display: block; margin: 0 auto; border: 0; background: none; padding: 6px;
    font: inherit; font-size: 13px; color: var(--indigo); cursor: pointer;
  }
  .auth-alt:hover { text-decoration: underline; text-underline-offset: 3px; }
  .auth-foot { margin-top: 18px; text-align: center; font-size: 13px; color: var(--muted); }
  .auth-foot button { padding: 0 2px; }

  /* Federated sign-in. Outline rather than filled, so the primary action stays
     the one the product actually recommends. */
  .auth-sep {
    display: flex; align-items: center; gap: 12px; margin: 18px 0;
    color: var(--faint); font-size: 12px;
  }
  /* Any author `display` beats the browser's own `[hidden] { display: none }`,
     so every rule above that sets one has to opt back out by hand. Without
     this the "or" divider shows on a deployment with no federated sign-in
     configured, separating the form from nothing. */
  .auth-sep[hidden], .auth-oauth[hidden], .auth-brand[hidden] { display: none; }
  .auth-sep::before, .auth-sep::after {
    content: ""; flex: 1; height: 1px; background: var(--line);
  }
  .auth-oauth {
    display: flex; align-items: center; justify-content: center; gap: 10px;
    width: 100%; padding: 11px; margin-bottom: 9px; background: #fff;
    border: 1px solid var(--line); border-radius: 11px; font: inherit;
    font-size: 14px; font-weight: 550; cursor: pointer;
  }
  .auth-oauth:hover { border-color: #d8d8e8; background: #fcfcfe; }
  .auth-oauth svg { width: 17px; height: 17px; flex: none; }

  .auth-msg { margin-top: 13px; font-size: 13px; min-height: 18px; text-align: center; }
  .auth-msg.bad { color: var(--err); }
  .auth-msg.good { color: #12855a; }
  .auth-msg.busy { color: var(--muted); }

  /* "Check your email" and other terminal states. */
  .auth-note {
    padding: 15px 16px; border-radius: 12px; background: #f6f6fb;
    border: 1px solid #e6e6f4; font-size: 13px; color: #414166; line-height: 1.5;
  }
  .auth-note b { font-weight: 600; }

  /* Onboarding: two questions and a skip, not a wizard. */
  .onb-choice { display: flex; flex-direction: column; gap: 8px; margin-bottom: 6px; }
  .onb-opt {
    display: flex; align-items: flex-start; gap: 11px; text-align: left;
    width: 100%; padding: 12px 14px; border: 1px solid var(--line);
    border-radius: 12px; background: #fff; cursor: pointer; font: inherit;
  }
  .onb-opt:hover { border-color: #d8d8e8; background: #fcfcfe; }
  .onb-opt[aria-pressed="true"] { border-color: var(--indigo); background: #f8f8ff; }
  /* Both are spans inside a span, so they need making block-level explicitly
     or the label and its description run together on one line. */
  .onb-opt .name { display: block; font-weight: 600; font-size: 14px; }
  .onb-opt .sub { display: block; font-size: 12.5px; color: var(--muted); margin-top: 2px; }

  /* The banner an unverified account sees inside the app. Same shape as the
     existing .needs-key warning, so it reads as the same class of nudge. */
  .verify-bar {
    display: flex; align-items: center; gap: 14px; margin: 0 0 12px;
    padding: 13px 15px; border-radius: 12px;
    background: #fdf3e3; border: 1px solid #f0dfc0;
  }
  .verify-bar[hidden] { display: none; }
  .verify-bar .body { flex: 1; min-width: 0; }
  .verify-bar .name { font-weight: 600; font-size: 13.5px; color: #6b4b12; }
  .verify-bar .sub { font-size: 12.5px; color: #7a5c22; margin-top: 2px; }
  .verify-bar button {
    flex: none; border: 0; border-radius: 999px; background: var(--dark);
    color: #fff; padding: 8px 15px; font: inherit; font-size: 12.5px;
    font-weight: 600; cursor: pointer;
  }
  .verify-bar button[disabled] { opacity: .55; cursor: default; }
"""


# ----------------------------------------------------------------------- html

# `hidden` on the overlay by default: the page decides on load whether to show
# it, and a flash of the sign-in screen for an already-signed-in user is the
# kind of detail that makes an app feel unfinished.
HTML = """
<div id="auth" hidden>
  <div class="auth-card">
    <div class="auth-brand">
      <span class="mark">
        <svg viewBox="0 0 24 24" fill="none"><path d="M3 12.5L20 5l-6.5 16-2.2-6.9z" fill="#fff"/></svg>
      </span>
      <span class="wordmark">Lumen OS</span>
    </div>

    <!-- sign in -->
    <div class="auth-view" id="viewLogin">
      <h1>Welcome back</h1>
      <p class="lede">Sign in to pick up where you left off.</p>

      <div id="oauthButtons"></div>
      <div class="auth-sep" id="oauthSep" hidden>or</div>

      <form id="loginForm" novalidate>
        <div class="field">
          <label for="loginEmail">Email</label>
          <input id="loginEmail" type="email" autocomplete="username"
                 inputmode="email" required>
        </div>
        <div class="field">
          <label for="loginPassword">Password</label>
          <div class="peek-wrap">
            <input id="loginPassword" type="password" autocomplete="current-password" required>
            <button type="button" class="peek" data-peek="loginPassword">Show</button>
          </div>
        </div>
        <div class="auth-row">
          <label><input type="checkbox" id="loginRemember" checked> Stay signed in</label>
          <button type="button" class="auth-alt" data-view="viewForgot">Forgot password?</button>
        </div>
        <button type="submit" class="auth-go" id="loginGo">Sign in</button>
      </form>
      <div class="auth-msg" id="loginMsg"></div>
      <div class="auth-foot">
        New here? <button type="button" class="auth-alt" data-view="viewSignup"
                          style="display:inline">Create an account</button>
      </div>
    </div>

    <!-- create account -->
    <div class="auth-view" id="viewSignup" hidden>
      <h1>Create your account</h1>
      <p class="lede">Your conversations, reminders and connections stay yours.</p>

      <form id="signupForm" novalidate>
        <div class="field">
          <label for="signupName">Name</label>
          <input id="signupName" type="text" autocomplete="name" required maxlength="120">
        </div>
        <div class="field">
          <label for="signupEmail">Email</label>
          <input id="signupEmail" type="email" autocomplete="username"
                 inputmode="email" required>
        </div>
        <div class="field">
          <label for="signupPassword">Password</label>
          <div class="peek-wrap">
            <input id="signupPassword" type="password" autocomplete="new-password" required>
            <button type="button" class="peek" data-peek="signupPassword">Show</button>
          </div>
          <div class="hint" id="signupHint">At least 12 characters.</div>
        </div>
        <button type="submit" class="auth-go" id="signupGo">Create account</button>
      </form>
      <div class="auth-msg" id="signupMsg"></div>
      <div class="auth-foot">
        Already have an account? <button type="button" class="auth-alt"
          data-view="viewLogin" style="display:inline">Sign in</button>
      </div>
    </div>

    <!-- check your email -->
    <div class="auth-view" id="viewVerify" hidden>
      <h1>Check your email</h1>
      <p class="lede">We have sent a confirmation link to <b id="verifyAddress"></b>.</p>
      <div class="auth-note">
        Click the link in that email, then come back and sign in. If it has not
        arrived in a minute or two, check your spam folder.
      </div>
      <div style="margin-top:16px">
        <button type="button" class="auth-go" id="verifyResend">Send it again</button>
      </div>
      <div class="auth-msg" id="verifyMsg"></div>
      <div class="auth-foot">
        <button type="button" class="auth-alt" data-view="viewLogin">Back to sign in</button>
      </div>
    </div>

    <!-- forgot password -->
    <div class="auth-view" id="viewForgot" hidden>
      <h1>Reset your password</h1>
      <p class="lede">Enter your email and we will send you a link.</p>
      <form id="forgotForm" novalidate>
        <div class="field">
          <label for="forgotEmail">Email</label>
          <input id="forgotEmail" type="email" autocomplete="username"
                 inputmode="email" required>
        </div>
        <button type="submit" class="auth-go" id="forgotGo">Send reset link</button>
      </form>
      <div class="auth-msg" id="forgotMsg"></div>
      <div class="auth-foot">
        <button type="button" class="auth-alt" data-view="viewLogin">Back to sign in</button>
      </div>
    </div>

    <!-- change password: the one view reached while signed in, from the
         account menu, which is why it is the only one with a way back -->
    <div class="auth-view" id="viewPassword" hidden>
      <h1>Change your password</h1>
      <p class="lede">You will stay signed in here. Other devices will not.</p>
      <form id="passwordForm" novalidate>
        <div class="field">
          <label for="pwCurrent">Current password</label>
          <div class="peek-wrap">
            <input id="pwCurrent" type="password" autocomplete="current-password" required>
            <button type="button" class="peek" data-peek="pwCurrent">Show</button>
          </div>
        </div>
        <div class="field">
          <label for="pwNew">New password</label>
          <div class="peek-wrap">
            <input id="pwNew" type="password" autocomplete="new-password" required>
            <button type="button" class="peek" data-peek="pwNew">Show</button>
          </div>
          <div class="hint">At least 12 characters.</div>
        </div>
        <button type="submit" class="auth-go" id="pwGo">Change password</button>
      </form>
      <div class="auth-msg" id="pwMsg"></div>
      <div class="auth-foot">
        <button type="button" class="auth-alt" id="pwCancel">Cancel</button>
      </div>
    </div>

    <!-- onboarding: two questions, both skippable -->
    <div class="auth-view" id="viewOnboard" hidden>
      <h1>One quick thing</h1>
      <p class="lede">How would you like the agent to reply?</p>
      <div class="onb-choice" id="onbStyles">
        <button type="button" class="onb-opt" data-style="default" aria-pressed="true">
          <span><span class="name">Balanced</span>
          <span class="sub">Clear answers, as much detail as the question needs.</span></span>
        </button>
        <button type="button" class="onb-opt" data-style="brief">
          <span><span class="name">Brief</span>
          <span class="sub">Short and direct. Good when you already know the context.</span></span>
        </button>
        <button type="button" class="onb-opt" data-style="detailed">
          <span><span class="name">Thorough</span>
          <span class="sub">More background and reasoning shown.</span></span>
        </button>
      </div>
      <div style="margin-top:18px">
        <button type="button" class="auth-go" id="onbGo">Continue</button>
      </div>
      <div class="auth-msg" id="onbMsg"></div>
      <div class="auth-foot">
        You can connect Gmail, Calendar and Drive later in Settings —
        the agent works without them.
      </div>
    </div>
  </div>
</div>
"""


# ------------------------------------------------------------------- behaviour

JS = r"""
/* ---------------------------------------------------------------- auth flow */

/* The page holds no token. Every call below goes to the loopback server, which
   attaches the Authorization header from the OS credential store. The most this
   script ever knows is whether somebody is signed in and what they are called. */

const authEl = document.getElementById('auth');
let authState = { signed_in: false };
let authConfig = { providers: { password: true } };
let pendingEmail = '';

function authShow(viewId) {
  /* Signing out and changing a password are both started from the account
     menu, and it does not close itself. Left open it would still be hanging
     there behind the overlay on the way back. */
  const menu = document.getElementById('acctMenu');
  if (menu) menu.hidden = true;
  const menuButton = document.getElementById('account');
  if (menuButton) menuButton.setAttribute('aria-expanded', 'false');

  for (const view of document.querySelectorAll('.auth-view')) {
    view.hidden = view.id !== viewId;
  }
  authEl.hidden = false;
  const first = document.querySelector('#' + viewId + ' input');
  if (first) setTimeout(() => first.focus(), 30);
}

function authHide() { authEl.hidden = true; }

function say(id, text, kind) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text || '';
  el.className = 'auth-msg' + (kind ? ' ' + kind : '');
}

/* One place that knows how to talk to the local server, so no screen has to
   remember the access key or the error shape. */
async function authCall(path, body, method) {
  /* Same convention as the rest of the page: the key rides in a header, and
     after a reload KEY is null because the query string was stripped from the
     address bar -- the cookie carries it then. Putting it in the URL instead
     would undo that, and would write the key into anything logging URLs. */
  const headers = { 'Content-Type': 'application/json' };
  if (KEY) headers['X-Agent-Key'] = KEY;

  const response = await fetch(path, {
    method: method || 'POST',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body || {}),
  });
  let payload = {};
  try { payload = await response.json(); } catch (e) { payload = {}; }
  if (!response.ok) {
    const error = payload.error || {};
    throw Object.assign(
      new Error(error.message || 'Something went wrong. Please try again.'),
      { code: error.code || 'error' }
    );
  }
  return payload;
}

function busyButton(id, isBusy, restingLabel) {
  const button = document.getElementById(id);
  if (!button) return;
  button.disabled = isBusy;
  button.textContent = isBusy ? 'Just a moment…' : restingLabel;
}

/* show/hide password, for every field that has a reveal button */
document.addEventListener('click', (event) => {
  const peek = event.target.closest('[data-peek]');
  if (peek) {
    const input = document.getElementById(peek.dataset.peek);
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    peek.textContent = showing ? 'Show' : 'Hide';
    input.focus();
    return;
  }
  const nav = event.target.closest('[data-view]');
  if (nav && nav.dataset.view.startsWith('view')) {
    authShow(nav.dataset.view);
  }
});

/* --- sign in --------------------------------------------------------- */

document.getElementById('loginForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const email = document.getElementById('loginEmail').value.trim();
  const password = document.getElementById('loginPassword').value;
  if (!email || !password) { say('loginMsg', 'Enter your email and password.', 'bad'); return; }

  busyButton('loginGo', true, 'Sign in');
  say('loginMsg', 'Signing you in…', 'busy');
  try {
    const result = await authCall('/api/auth/login', {
      email, password, remember: document.getElementById('loginRemember').checked,
    });
    authState = result;
    say('loginMsg', '', '');
    afterSignIn(result);
  } catch (error) {
    /* An unverified account is the one failure with somewhere useful to go. */
    if (error.code === 'email_not_verified') {
      pendingEmail = email;
      document.getElementById('verifyAddress').textContent = email;
      authShow('viewVerify');
      say('verifyMsg', '', '');
    } else {
      say('loginMsg', error.message, 'bad');
    }
  } finally {
    busyButton('loginGo', false, 'Sign in');
  }
});

/* --- create account -------------------------------------------------- */

document.getElementById('signupForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const name = document.getElementById('signupName').value.trim();
  const email = document.getElementById('signupEmail').value.trim();
  const password = document.getElementById('signupPassword').value;
  if (!name || !email || !password) {
    say('signupMsg', 'Fill in every field.', 'bad'); return;
  }

  busyButton('signupGo', true, 'Create account');
  say('signupMsg', 'Creating your account…', 'busy');
  try {
    const result = await authCall('/api/auth/signup', { name, email, password });
    pendingEmail = email;
    if (result.status === 'signed_in') {
      authState = result;
      afterSignIn(result);
    } else {
      document.getElementById('verifyAddress').textContent = email;
      authShow('viewVerify');
    }
  } catch (error) {
    say('signupMsg', error.message, 'bad');
  } finally {
    busyButton('signupGo', false, 'Create account');
  }
});

/* --- verification ---------------------------------------------------- */

document.getElementById('verifyResend').addEventListener('click', async () => {
  busyButton('verifyResend', true, 'Send it again');
  try {
    const result = await authCall('/api/auth/resend', { email: pendingEmail });
    say('verifyMsg', result.message || 'Sent.', 'good');
  } catch (error) {
    say('verifyMsg', error.message, 'bad');
  } finally {
    busyButton('verifyResend', false, 'Send it again');
  }
});

/* --- forgot password ------------------------------------------------- */

document.getElementById('forgotForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const email = document.getElementById('forgotEmail').value.trim();
  if (!email) { say('forgotMsg', 'Enter your email.', 'bad'); return; }

  busyButton('forgotGo', true, 'Send reset link');
  try {
    const result = await authCall('/api/auth/forgot', { email });
    /* Says the same thing whether or not the address has an account. */
    say('forgotMsg', result.message || 'Check your email.', 'good');
  } catch (error) {
    say('forgotMsg', error.message, 'bad');
  } finally {
    busyButton('forgotGo', false, 'Send reset link');
  }
});

/* --- change password ------------------------------------------------- */

/* Opened from the account menu, so unlike every other view it is entered from
   inside the app and has to be able to put the app back. */
function openPasswordChange() {
  for (const id of ['pwCurrent', 'pwNew']) document.getElementById(id).value = '';
  say('pwMsg', '', '');
  authShow('viewPassword');
}

function closePasswordChange() {
  /* Not left in the DOM for the rest of the session. */
  for (const id of ['pwCurrent', 'pwNew']) document.getElementById(id).value = '';
  authHide();
}

document.getElementById('pwCancel').addEventListener('click', closePasswordChange);

document.getElementById('passwordForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const current = document.getElementById('pwCurrent').value;
  const next = document.getElementById('pwNew').value;
  if (!current || !next) { say('pwMsg', 'Fill in both fields.', 'bad'); return; }

  busyButton('pwGo', true, 'Change password');
  try {
    const result = await authCall('/api/auth/password', {
      current_password: current, new_password: next,
    });
    /* The server's wording, because it is the side that knows the other
       devices were signed out. */
    say('pwMsg', result.message || 'Password updated.', 'good');
    document.getElementById('pwCurrent').value = '';
    document.getElementById('pwNew').value = '';
    setTimeout(closePasswordChange, 2200);
  } catch (error) {
    say('pwMsg', error.message, 'bad');
  } finally {
    busyButton('pwGo', false, 'Change password');
  }
});

/* --- onboarding ------------------------------------------------------ */

let onboardStyle = 'default';
document.getElementById('onbStyles').addEventListener('click', (event) => {
  const option = event.target.closest('[data-style]');
  if (!option) return;
  onboardStyle = option.dataset.style;
  for (const button of document.querySelectorAll('#onbStyles .onb-opt')) {
    button.setAttribute('aria-pressed', String(button === option));
  }
});

document.getElementById('onbGo').addEventListener('click', async () => {
  busyButton('onbGo', true, 'Continue');
  try {
    await authCall('/api/auth/onboarding', {
      response_style: onboardStyle,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      complete: true,
    });
    authHide();
    enterApp();
  } catch (error) {
    say('onbMsg', error.message, 'bad');
  } finally {
    busyButton('onbGo', false, 'Continue');
  }
});

/* --- federated sign-in ----------------------------------------------- */

const GOOGLE_MARK = '<svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.5 12.2c0-.8-.1-1.4-.2-2H12v3.9h6a5 5 0 0 1-2.2 3.3v2.7h3.5c2-1.9 3.2-4.7 3.2-7.9z"/><path fill="#34A853" d="M12 23c2.9 0 5.4-1 7.2-2.6l-3.5-2.7c-1 .6-2.2 1-3.7 1-2.9 0-5.3-1.9-6.2-4.5H2.2v2.8A11 11 0 0 0 12 23z"/><path fill="#FBBC05" d="M5.8 14.2a6.6 6.6 0 0 1 0-4.2V7.2H2.2a11 11 0 0 0 0 9.8l3.6-2.8z"/><path fill="#EA4335" d="M12 4.8c1.6 0 3 .6 4.1 1.6l3.1-3.1A11 11 0 0 0 2.2 7.2l3.6 2.8C6.7 7.4 9.1 4.8 12 4.8z"/></svg>';
const APPLE_MARK = '<svg viewBox="0 0 24 24"><path fill="currentColor" d="M16.4 12.8c0-2.3 1.9-3.4 2-3.5-1.1-1.6-2.8-1.8-3.4-1.8-1.4-.1-2.8.9-3.5.9s-1.8-.9-3-.8c-1.5 0-2.9.9-3.7 2.3-1.6 2.7-.4 6.8 1.1 9 .8 1.1 1.6 2.3 2.8 2.2 1.1 0 1.6-.7 2.9-.7s1.7.7 2.9.7 2-1.1 2.7-2.2c.9-1.2 1.2-2.4 1.2-2.5 0 0-2.4-.9-2.4-3.6zM14.2 5.7c.6-.8 1-1.9.9-3-.9 0-2 .6-2.7 1.4-.6.7-1.1 1.8-.9 2.9 1 0 2.1-.5 2.7-1.3z"/></svg>';

function drawOAuthButtons() {
  const host = document.getElementById('oauthButtons');
  const providers = authConfig.providers || {};
  const buttons = [];
  if (providers.google) {
    buttons.push('<button type="button" class="auth-oauth" data-oauth="google">'
      + GOOGLE_MARK + 'Continue with Google</button>');
  }
  if (providers.apple) {
    buttons.push('<button type="button" class="auth-oauth" data-oauth="apple">'
      + APPLE_MARK + 'Continue with Apple</button>');
  }
  host.innerHTML = buttons.join('');
  document.getElementById('oauthSep').hidden = buttons.length === 0;
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-oauth]');
  if (!button) return;
  try {
    /* The server opens the system browser: a federated sign-in must happen
       somewhere the user can see the address bar and check the padlock. */
    await authCall('/api/auth/oauth', { provider: button.dataset.oauth });
    say('loginMsg', 'Finish signing in in your browser, then come back.', 'busy');
  } catch (error) {
    say('loginMsg', error.message, 'bad');
  }
});

/* --- session lifecycle ----------------------------------------------- */

function afterSignIn(state) {
  authState = state;
  const account = state.account || {};
  if (state.needs_onboarding) {
    authShow('viewOnboard');
    return;
  }
  authHide();
  enterApp();
}

/* Draws the unverified-account banner, and the account name in the rail. */
function enterApp() {
  const account = (authState && authState.account) || {};

  /* The rail's account line is the page's own #acct element; it showed the
     Google address before there were Lumen accounts, and now leads with the
     signed-in user. */
  const nameEl = document.getElementById('acct');
  if (nameEl && (account.display_name || account.email)) {
    nameEl.textContent = account.display_name || account.email;
  }
  const roleEl = document.getElementById('acctrole');
  if (roleEl && account.email) roleEl.textContent = account.email;

  const bar = document.getElementById('verifyBar');
  if (bar) bar.hidden = !(account.email && account.email_verified === false);
}

async function signOut(everywhere) {
  try { await authCall('/api/auth/logout', { everywhere: !!everywhere }); }
  catch (error) { /* local state is cleared regardless */ }
  authState = { signed_in: false };
  authShow('viewLogin');
}

for (const [id, everywhere] of [['lumenSignout', false], ['lumenSignoutAll', true]]) {
  const button = document.getElementById(id);
  if (button) button.addEventListener('click', () => signOut(everywhere));
}

const passwordButton = document.getElementById('lumenPassword');
if (passwordButton) passwordButton.addEventListener('click', openPasswordChange);

/* Resend from the in-app banner, as opposed to the one on the verify screen. */
const verifyBarSend = document.getElementById('verifyBarSend');
if (verifyBarSend) {
  verifyBarSend.addEventListener('click', async () => {
    const account = (authState && authState.account) || {};
    verifyBarSend.disabled = true;
    const original = verifyBarSend.textContent;
    try {
      await authCall('/api/auth/resend', { email: account.email });
      verifyBarSend.textContent = 'Sent';
    } catch (error) {
      verifyBarSend.textContent = 'Try again';
    } finally {
      setTimeout(() => {
        verifyBarSend.disabled = false;
        verifyBarSend.textContent = original;
      }, 4000);
    }
  });
}

/* Called on load, and whenever a request comes back 401. */
async function refreshAuthState() {
  try {
    const state = await authCall('/api/auth/state', undefined, 'GET');
    authConfig = state.config || authConfig;
    drawOAuthButtons();
    authState = state;
    if (!state.signed_in) { authShow('viewLogin'); return false; }
    if (state.needs_onboarding) { authShow('viewOnboard'); return false; }
    authHide();
    enterApp();
    return true;
  } catch (error) {
    authShow('viewLogin');
    return false;
  }
}

/* An expired session anywhere in the app returns here rather than leaving a
   dead screen behind. */
window.addEventListener('lumen-unauthenticated', () => {
  authState = { signed_in: false };
  say('loginMsg', 'Your session has expired. Please sign in again.', 'bad');
  authShow('viewLogin');
});

refreshAuthState();
"""


# The account menu's Lumen entries. Injected rather than written into the page
# so that with no accounts server there is no menu item pointing at one.
MENU_ITEMS = """
<button id="lumenPassword">Change password</button>
<button id="lumenSignout">Sign out of Lumen</button>
<button id="lumenSignoutAll">Sign out of all devices</button>
"""


# The banner shown inside the app to an account that has not confirmed its
# address yet. Injected near the top of the panel rather than as an overlay:
# an unverified user can still use the agent, they just cannot connect
# integrations, so nagging them with a full-screen block would be wrong.
VERIFY_BAR = """
<div class="verify-bar" id="verifyBar" hidden>
  <div class="body">
    <div class="name">Confirm your email address</div>
    <div class="sub">Connecting Gmail, Drive and Calendar stays locked until you do.</div>
  </div>
  <button type="button" id="verifyBarSend">Resend</button>
</div>
"""
