class BrowserError(RuntimeError):
    """Base error for user-actionable browser runtime failures."""


class BrowserAuthRequired(BrowserError):
    """Raised when ChatGPT requires interactive human authentication."""
