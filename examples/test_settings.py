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


# -------------------------
# 1. Open System Settings
# -------------------------

print("Opening System Settings...")

subprocess.run(
    [
        "open",
        "-a",
        "System Settings",
    ],
    check=True,
)

time.sleep(4)


# -------------------------
# 2. Set up arc_cua
# -------------------------

executor = DesktopExecutor(
    backend=MacOSHybridBackend(
        ocr_recognition_level="fast",
        ocr_min_confidence=0.45,
    ),
    policy=TypeSafeJevPolicy(),
)


# -------------------------
# 3. Agent-supplied subtask
# -------------------------

task = Subtask(
    goal=(
        "In macOS System Settings, navigate to Appearance "
        "and change the system appearance to Dark."
    ),

    # Literal text the agent has already decided is useful.
    # JEV may choose this input_key but cannot invent other text.
    inputs={
        "search_query": "Appearance",
    },

    verification=(
        "System Settings is showing the Appearance settings.",
        "Dark is selected as the current system appearance.",
    ),

    constraints=(
        "Do not modify any setting other than the system appearance.",
        "Do not change the accent color.",
        "Do not change highlight color.",
    ),

    max_actions=15,
)


# -------------------------
# 4. Run
# -------------------------

print("\nTask: change macOS appearance to Dark")
print("Starting arc_cua...\n")

result = executor.run(task)


# -------------------------
# 5. Trace
# -------------------------

print("\n==============================")
print("RESULT")
print("==============================")

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


print("\n==============================")
print("TRACE")
print("==============================")

for record in result.history:
    print(
        record.compact()
    )
