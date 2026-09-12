import json
import os

from scripthaul import ScriptHaul

client = ScriptHaul()
channel = os.environ.get("SCRIPTHAUL_CHANNEL", "@TED")
query = os.environ.get("SCRIPTHAUL_QUERY", '"solar energy"')
coverage = client.library.coverage(channel=channel)
passages = client.library.search(query, channel=channel, limit=10)
created = client.monitors.create(channel=channel, auto_fetch=False)
try:
    listed = client.monitors.list()
    current = client.monitors.get(created["monitor"]["id"])
finally:
    cancelled = client.monitors.cancel(created["monitor"]["id"])
print(json.dumps({"complete": coverage["complete"], "indexed": coverage["indexed"],
                  "passages": passages["results"], "coverage": passages.meta.library_coverage,
                  "monitor_listed": any(monitor["id"] == created["monitor"]["id"] for monitor in listed["monitors"]),
                  "monitor_status": current["monitor"]["status"], "cancelled": cancelled["cancelled"],
                  "credits_charged": coverage.meta.credits_charged + passages.meta.credits_charged + created.meta.credits_charged}))
