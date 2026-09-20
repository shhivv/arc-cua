import subprocess
import time

from arc_cua import (
    DesktopExecutor,
    Subtask,
)

from arc_cua.backends import (
    MacOSHybridBackend,
)

from arc_cua.policies import (
    TypeSafeJevPolicy,
)


ARTIST = "Daft Punk"
SONG = "Get Lucky"

SEARCH_QUERY = (
    f"{SONG} {ARTIST}"
)


print("Opening Spotify...")

subprocess.run(
    [
        "open",
        "-a",
        "Spotify",
    ],
    check=True,
)

time.sleep(5)


executor = DesktopExecutor(

    backend=MacOSHybridBackend(
        ocr_recognition_level="fast",
        ocr_min_confidence=0.45,
    ),

    policy=TypeSafeJevPolicy(),
)


subtask = Subtask(

    goal=(
        f"In Spotify, find "
        f"'{SONG}' by {ARTIST} "
        "and start playing it."
    ),

    inputs={
        "artist": ARTIST,
        "song": SONG,
        "search_query": SEARCH_QUERY,
    },

    verification=(
        f"Spotify shows '{SONG}' "
        f"by {ARTIST} as the "
        "currently playing track.",
    ),

    constraints=(
        "Do not like or unlike anything.",
        "Do not add anything to a playlist.",
        "Do not modify the library.",
        "Do not change account settings.",
    ),

    max_actions=20,
)


result = executor.run(
    subtask
)


print("\n=== RESULT ===")

print(
    "status:",
    result.status.value,
)

print(
    "actions:",
    result.actions_taken,
)

if result.reason:
    print(
        "reason:",
        result.reason,
    )


print("\n=== TRACE ===")

for record in result.history:
    print(
        record.compact()
    )
