import time

from arc_cua.backends import (
    MacOSHybridBackend,
)


print(
    "Switch to the app you want to inspect. "
    "Capturing in 4 seconds..."
)

time.sleep(4)

backend = MacOSHybridBackend()

snapshot = backend.observe()

print(
    f"\n{snapshot.application} "
    f"— {snapshot.window}"
)

print(
    f"total elements: "
    f"{len(snapshot.elements)}"
)

ocr = [
    element
    for element in snapshot.elements
    if element.source == "macos_ocr"
]

print(
    f"OCR elements: {len(ocr)}\n"
)

for element in ocr:

    bounds = element.bounds

    print(
        f"{element.id}  "
        f"{element.name!r}  "
        f"conf="
        f"{element.metadata.get('confidence')}  "
        f"bounds=("
        f"{bounds.x:.0f}, "
        f"{bounds.y:.0f}, "
        f"{bounds.width:.0f}, "
        f"{bounds.height:.0f})"
    )
