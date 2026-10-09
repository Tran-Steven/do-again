class BrowserError(RuntimeError):
    """Base error for user-actionable browser runtime failures."""


class BrowserSubmissionUncertain(BrowserError):
    """A browser submission may have succeeded, but its result was not verified."""


class BrowserAuthRequired(BrowserError):
    """Raised when ChatGPT requires interactive human authentication."""
