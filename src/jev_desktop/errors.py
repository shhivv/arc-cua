class JevDesktopError(RuntimeError):
    pass


class StaleDesktopState(JevDesktopError):
    """The chosen target no longer means what it meant when observed."""


class InvalidDecision(JevDesktopError):
    """The policy returned an action that is not legal in the current snapshot."""


class UnsupportedDesktopAction(JevDesktopError):
    pass
