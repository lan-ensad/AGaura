"""User-facing messages for service error codes and flash notices.

Strings are marked with ``N_`` and translated when displayed. Each part of the web application
may add its own codes in its own module; ``error_message`` falls back to a generic message.
"""

from __future__ import annotations

from agaura.i18n import N_, _
from agaura.services.errors import ServiceError

ERRORS: dict[str, str] = {
    # Generic
    "not_found": N_("This page does not exist or is not available."),
    "required": N_("This field is required."),
    "too_long": N_("This text is too long."),
    "control_characters": N_("This text contains invisible control characters."),
    # Sessions
    "invalid_quorum_rule": N_("Enter the quorum as a number of people (50) or a share (1/4)."),
    "invalid_registered_count": N_("The number of registered members must be at least 1."),
    "assembly_preparation": N_("The session has not started yet."),
    "assembly_in_session": N_("Not possible while the session is in session."),
    "assembly_closed": N_("The session is closed."),
    "polls_still_open": N_("Close the open polls before closing the session."),
    "access_mode_poll_open": N_(
        "Close the open polls before changing the access mode: "
        "someone who already voted could vote again."
    ),
    "codes_poll_open": N_(
        "Close the open polls before generating codes: someone who already voted could vote "
        "again with a new code."
    ),
    "open_assemblies": N_("You have reached the maximum number of sessions not yet closed."),
    "polls_per_assembly": N_("This session has reached the maximum number of polls."),
    "codes_per_assembly": N_(
        "This session has reached the maximum number of codes, revoked codes included."
    ),
    # Access
    "session_not_in_session": N_("This session is not open for voting."),
    "entries_locked": N_("Entries are closed. Ask the tribune to let you in."),
    "access_cap_reached": N_(
        "This session has reached its maximum number of accesses: no new device can join. "
        "Ask the tribune."
    ),
    "demo_voters_full": N_(
        "This demonstration session takes 5 voters at most, and they have all joined: no new "
        "device can join it. A real assembly needs an account."
    ),
    "wrong_secret": N_("Incorrect password or code."),
    "invalid_credential": N_("Your access to this session is no longer valid. Join again."),
    "too_many_attempts": N_("Too many attempts. Wait a few minutes and try again."),
    "invalid_headcount": N_("The headcount must be a positive number."),
    "headcount_too_large": N_(
        "The headcount cannot exceed the number of codes a session may hold."
    ),
    "invalid_batch_size": N_("The number of codes must be between 1 and 2,000."),
    "password_too_long": N_("This password is too long."),
    # Polls
    "poll_draft": N_("This poll has not been pushed yet."),
    "poll_open": N_("This poll is open."),
    "poll_closed": N_("This poll is closed."),
    "poll_published": N_("The result of this poll is published."),
    "poll_cancelled": N_("This poll was cancelled."),
    "poll_not_open": N_("Voting on this poll is closed."),
    "invalid_timer": N_("The timer must be between 1 and 1,440 minutes."),
    "invalid_order": N_("The order of the drafts has changed meanwhile; reload the page."),
    "no_target": N_("Choose where to show the result."),
    "duplicate_option_label": N_("Two options have the same label."),
    "option_count": N_("A poll needs between 2 and 30 options."),
    "unknown_method": N_("Unknown voting method."),
    "invalid_param": N_("A setting of the poll is invalid."),
    "missing_param": N_("A setting of the poll is missing."),
    "unknown_param": N_("A setting of the poll is invalid."),
    # Ballots
    "already_voted": N_("Your vote has already been recorded."),
    "malformed_ballot": N_("This ballot could not be read. Reload the page and vote again."),
    "unknown_option": N_("This ballot contains an unknown option."),
    "empty_ballot": N_("Choose at least one option, or vote blank."),
    "duplicate_option": N_("An option was chosen twice."),
    "too_many_choices": N_("Too many options checked."),
    "invalid_grade": N_("A grade is invalid."),
    "invalid_rank": N_("A rank is invalid."),
    # Accounts
    "invalid_email": N_("This email address is invalid."),
    "email_taken": N_("An account already exists with this email address."),
    "password_too_short": N_("The password is too short."),
    "invalid_login": N_("Incorrect email or password."),
    "login_pow_required": N_(
        "After several failed logins with this address, an anti-robot check is needed. Wait "
        "until it is complete, then enter your password again."
    ),
}

NOTICES: dict[str, str] = {
    "logged_in": N_("You are logged in."),
    "logged_out": N_("You are logged out."),
    "left_session": N_("You have left the session on this device."),
}

GENERIC_ERROR = N_("This action is not possible.")


def error_message(error: ServiceError | str) -> str:
    code = error if isinstance(error, str) else error.code
    return _(ERRORS.get(code, GENERIC_ERROR))
