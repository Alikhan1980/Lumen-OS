"""The desktop client's authentication.

    session.py   the signed-in session: tokens, refresh, authenticated calls
    screens.py   the sign-in / sign-up / onboarding UI, injected into the page
    routes.py    the loopback endpoints the page calls

The division of labour worth remembering: the API server decides *everything*
about authorisation, and this package decides only where the desktop keeps its
credentials and what the user sees. No check performed here is load-bearing --
if the page were bypassed entirely, the server would still refuse.

None of it is reached unless `accounts_enabled()`. With no `LUMEN_API_URL` there
is no deployment to have an account with, and the app is the single-user desktop
program it was before this package existed.
"""

from .session import (
    Account,
    AuthError,
    Session,
    SessionExpired,
    accounts_enabled,
    shared,
)

__all__ = [
    "Account",
    "AuthError",
    "Session",
    "SessionExpired",
    "accounts_enabled",
    "shared",
]
