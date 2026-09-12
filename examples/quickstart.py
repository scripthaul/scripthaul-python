import json
import os

from scripthaul import ScriptHaul


client = ScriptHaul(
    api_key=os.environ.get("SCRIPTHAUL_API_KEY"),
    base_url=os.environ.get("SCRIPTHAUL_BASE_URL"),
    poll_interval=float(os.environ.get("SCRIPTHAUL_POLL_SECONDS", "1")),
    job_poll_interval=float(os.environ.get("SCRIPTHAUL_POLL_SECONDS", "2")),
    job_timeout=float(os.environ.get("SCRIPTHAUL_JOB_TIMEOUT_SECONDS", "600")),
)
video_id = os.environ.get("SCRIPTHAUL_VIDEO_ID", "dQw4w9WgXcQ")
input_url = os.environ.get("SCRIPTHAUL_INPUT")
if not input_url:
    raise RuntimeError(
        "Set SCRIPTHAUL_INPUT to a public YouTube channel or playlist URL.")
# Spend bound for live runs: pin the job to listed videos (they must belong to
# SCRIPTHAUL_INPUT) and refuse to wait on a job larger than the cap.
job_video_ids = [value.strip() for value in
                 os.environ.get("SCRIPTHAUL_JOB_VIDEO_IDS", "").split(",") if value.strip()]
max_job_videos = int(os.environ.get("SCRIPTHAUL_MAX_JOB_VIDEOS", "5"))

transcript = client.transcript(video_id, format="clean")
listing = client.videos(input_url, contents="videos")
options = {"format": "clean"}
if job_video_ids:
    options["video_ids"] = job_video_ids
job = client.jobs.create(
    input=input_url,
    idempotency_key=os.environ.get("SCRIPTHAUL_IDEMPOTENCY_KEY") or None,
    **options,
)
total = (job.data.get("counts") or {}).get("total", 0)
if total > max_job_videos:
    job.cancel()
    raise RuntimeError("Job %s spans %s videos; the quickstart bound is %s. Set SCRIPTHAUL_JOB_VIDEO_IDS."
                       % (job.id, total, max_job_videos))
job.wait()
if job.status != "completed":
    raise RuntimeError("Job %s ended as %s." % (job.id, job.status))

print(json.dumps({
    "transcript": transcript["video"]["id"],
    "cache": transcript.meta.cache,
    "videos": len(listing["videos"]),
    "job": job.id,
    "status": job.status,
}, separators=(",", ":")))
