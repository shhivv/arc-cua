from .memory import StateMachineBackend

__all__ = ["StateMachineBackend"]

# Import lazily on macOS users' machines; importing the module itself is safe on
# other platforms because PyObjC is loaded only when the backend is instantiated.
from .macos_ax import MacOSAXBackend

__all__.append("MacOSAXBackend")
