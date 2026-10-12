class BrowserError(RuntimeError):
    """Base error for user-actionable browser runtime failures."""


class BrowserPreDispatchBlocked(BrowserError):
    """A verified pre-Send blocker: no ChatGPT Send gesture was attempted.

    Only raise when the trusted transport can prove it never crossed the
    durable dispatch boundary. An ambiguous CDP result after that boundary
    must use BrowserSubmissionUncertain, never this type.
    """


class BrowserSubmissionUncertain(BrowserError):
    """A browser submission may have succeeded, but its result was not verified."""


class BrowserAuthRequired(BrowserError):
    """Raised when ChatGPT requires interactive human authentication."""
