"""Print a compact semantic snapshot of the frontmost macOS app.

Usage:
  pip install -e '.[macos]'
  python examples/macos_ax_probe.py

Grant your terminal/Python host Accessibility permission first.
"""

from jev_desktop.backends import MacOSAXBackend


backend = MacOSAXBackend()
snapshot = backend.observe()
print(f"{snapshot.application} — {snapshot.window} — {len(snapshot.elements)} elements")
for element in snapshot.elements[:150]:
    print(element.compact())
