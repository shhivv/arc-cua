import time

from arc_cua import DesktopExecutor, Subtask
from arc_cua.backends import MacOSAXBackend
from arc_cua.policies import TypeSafeJevPolicy


print("Switch to Apple Calendar.")
print("Starting in 5 seconds...")
time.sleep(5)

backend = MacOSAXBackend()
policy = TypeSafeJevPolicy()

executor = DesktopExecutor(
    backend=backend,
    policy=policy,
)

task = Subtask(
    goal=(
        "In Apple Calendar, create a new event named 'Meet with Sam' "
        "for today at 7:00 PM."
    ),

    inputs={
        "quick_event_text": "Meet with Sam tooday at 7 PM",
        "event_title": "Meet with Sam",
        "event_time": "7:00 PM",
    },

    verification=(
        "An event named 'Meet with Sam' exists today at 7:00 PM.",
    ),

    constraints=(
        "Do not modify or delete any existing events.",
        "Create only one new event.",
    ),

    max_actions=12,
)

result = executor.run(task)

print("\n=== RESULT ===")
print("status:", result.status)
print("actions:", result.actions_taken)

if result.reason:
    print("reason:", result.reason)

print("\n=== TRACE ===")

for record in result.history:
    print(record.compact())
