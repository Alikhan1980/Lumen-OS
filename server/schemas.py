"""Request and response models.

Validation here is the first server-side check on everything that arrives, and
it runs before a handler sees the value -- which is the useful property. Length
caps in particular are cheap denial-of-service protection: without a max on the
password field, a caller can make bcrypt hash a megabyte.

Nothing in this file echoes an input back in an error. See the validation
handler in errors.py for why.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

# RFC 5321 caps a path at 254 characters. Anything longer is not an address.
Email = Annotated[EmailStr, Field(max_length=254)]
# The cap matches passwords.MAX_LENGTH; the floor is 1 so that "too short" is
# reported by the policy with a helpful message rather than as a 422.
Password = Annotated[str, Field(min_length=1, max_length=128)]


class Strict(BaseModel):
    """Reject unknown fields rather than ignoring them.

    A client sending `{"email": ..., "user_id": ...}` is either confused or
    probing. Either way, silently dropping the extra field is how a mass
    assignment bug starts.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------------ auth in


class SignupIn(Strict):
    name: Annotated[str, Field(min_length=1, max_length=120)]
    email: Email
    password: Password

    @field_validator("name")
    @classmethod
    def _no_control_characters(cls, value: str) -> str:
        if any(ord(char) < 32 for char in value):
            raise ValueError("name contains control characters")
        return value


class LoginIn(Strict):
    email: Email
    password: Password
    # Drives the refresh cookie's lifetime: a browser session cookie when off,
    # a persistent one when on. The access token is short-lived either way.
    remember: bool = True


class RefreshIn(Strict):
    # Optional because a browser sends the refresh token in an httpOnly cookie
    # instead. The desktop client, which has no cookie jar worth the name, posts
    # it from the OS credential store.
    refresh_token: Annotated[str, Field(max_length=2048)] | None = None


class LogoutIn(Strict):
    # "global" ends every session everywhere. "local" ends this one.
    scope: Literal["local", "global", "others"] = "local"


class ForgotPasswordIn(Strict):
    email: Email


class ResetPasswordIn(Strict):
    """Completing a reset from the emailed link.

    The token is GoTrue's, single-use and expiring on its side. We never
    generate, store or validate one ourselves.
    """

    email: Email
    token: Annotated[str, Field(min_length=6, max_length=512)]
    password: Password


class ChangePasswordIn(Strict):
    current_password: Password
    new_password: Password


class ResendVerificationIn(Strict):
    email: Email


# --------------------------------------------------------------- profile in


class ProfileIn(Strict):
    display_name: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    avatar_url: Annotated[str, Field(max_length=2048)] | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=64)] | None = None

    @field_validator("avatar_url")
    @classmethod
    def _https_only(cls, value: str | None) -> str | None:
        if value and not value.startswith("https://"):
            # A javascript: or data: URL here would be rendered by the client.
            raise ValueError("avatar_url must be https")
        return value

    @field_validator("timezone")
    @classmethod
    def _real_zone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown timezone") from exc
        return value


class PreferencesIn(Strict):
    response_style: Literal["default", "brief", "detailed", "friendly", "formal"] | None = None
    show_thinking: bool | None = None
    auto_approve_tools: bool | None = None
    email_notifications: bool | None = None
    reminder_push: bool | None = None
    weekly_digest: bool | None = None


class DeleteAccountIn(Strict):
    """Deleting an account asks for the password again.

    Not theatre: it is the difference between "somebody walked past an unlocked
    laptop" and "the account holder decided". The confirmation phrase is a
    second, deliberate speed bump for an irreversible action.
    """

    password: Password
    confirmation: str

    @field_validator("confirmation")
    @classmethod
    def _must_match(cls, value: str) -> str:
        if value.strip().lower() != "delete my account":
            raise ValueError("confirmation phrase does not match")
        return value


class OnboardingIn(Strict):
    timezone: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    response_style: Literal["default", "brief", "detailed", "friendly", "formal"] | None = None
    complete: bool = False
