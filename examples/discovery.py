"""Find videos, resolve a channel, and read its newest uploads."""
import json
import os

from scripthaul import ScriptHaul

client = ScriptHaul()
query = os.environ.get("SCRIPTHAUL_QUERY", "public domain")
channel_input = os.environ.get("SCRIPTHAUL_CHANNEL", "@YouTube")
channel = client.channels.resolve(channel_input)
page = client.search(query)
channel_page = client.search(query, channel=channel_input)
latest = client.channels.latest(channel=channel["channel_id"])
playlist_latest = client.channels.latest(playlist=channel["uploads_playlist_id"])

print(json.dumps({
    "channel_id": channel["channel_id"], "results": len(page["results"]),
    "channel_results": len(channel_page["results"]), "continuation": page["continuation"],
    "latest": [video["video_id"] for video in latest["videos"]],
    "playlist_latest": [video["video_id"] for video in playlist_latest["videos"]],
    "credits_charged": page.meta.credits_charged + channel_page.meta.credits_charged,
    "latest_credits_charged": latest.meta.credits_charged + playlist_latest.meta.credits_charged,
}))
