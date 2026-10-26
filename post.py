"""Post due Instagram Reels from queue.json. Runs on GitHub Actions every 10 minutes, so posts go out
with the Mac off. Instagram's API can only publish now: this job is the clock. Each reel's video and
cover are fetched by Instagram from this repository's GitHub Pages site (the media branch).

Environment: IG_ACCESS_TOKEN (secret), IG_USER_ID and MEDIA_BASE (repository variables);
CHECK=N processes reel N without publishing it (a dry run). Standard library only."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

GRAPH = "https://graph.instagram.com/v23.0"
LATE = timedelta(hours=6)  # GitHub's clock can lag; later than this, a reel waits for the owner instead
ATTEMPTS = 3
QUEUE = Path(__file__).with_name("queue.json")
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")


def call(method: str, path: str, **fields) -> dict:
    data = urllib.parse.urlencode({**fields, "access_token": TOKEN})
    url = f"{GRAPH}/{path}" + ("" if method == "POST" else "?" + data)
    request = urllib.request.Request(url, data=data.encode() if method == "POST" else None, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace").replace(TOKEN, "***") if TOKEN else ""
        raise RuntimeError(f"Instagram HTTP {e.code}: {body[:400]}") from None


def reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError):
        return False


def post(reel: dict, base: str, user: str, publish: bool = True) -> dict:
    video, cover = f"{base}/{reel['video']}", f"{base}/{reel['cover']}"
    if not (reachable(video) and reachable(cover)):
        raise RuntimeError(f"media not reachable yet at {video}")
    fields = {"media_type": "REELS", "video_url": video, "cover_url": cover, "caption": reel["caption"],
              "share_to_feed": "true", "is_ai_generated": "true"}
    try:
        container = call("POST", f"{user}/media", **fields)
    except RuntimeError as e:
        if "is_ai_generated" not in str(e):
            raise
        fields.pop("is_ai_generated")
        container = call("POST", f"{user}/media", **fields)
    for _ in range(90):  # up to 15 minutes of processing
        state = call("GET", container["id"], fields="status_code,status")
        if state.get("status_code") == "FINISHED":
            break
        if state.get("status_code") in {"ERROR", "EXPIRED"}:
            raise RuntimeError(f"Instagram could not process the video: {state}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram was still processing after 15 minutes")
    if not publish:
        return {"container": container["id"]}
    media = call("POST", f"{user}/media_publish", creation_id=container["id"])
    link = call("GET", media["id"], fields="permalink").get("permalink", "")
    return {"media_id": media["id"], "permalink": link}


def main() -> int:
    if not TOKEN:
        print("IG_ACCESS_TOKEN secret is not set: nothing can post yet")
        return 1
    base, user = os.environ["MEDIA_BASE"].rstrip("/"), os.environ["IG_USER_ID"]
    queue = json.loads(QUEUE.read_text())
    if os.environ.get("CHECK"):
        want = os.environ["CHECK"]
        reel = next(r for r in queue if r.get("id") == want or r["number"] == want.zfill(2))
        print("dry run (not published):", reel["title"], post(reel, base, user, publish=False))
        return 0
    now, failed = datetime.now(UTC), False
    for reel in queue:
        if reel["status"] != "waiting" or datetime.fromisoformat(reel["at"]) > now:
            continue
        if now - datetime.fromisoformat(reel["at"]) > LATE:
            reel["status"], reel["note"] = "missed", f"not posted by {now.isoformat()} (more than 3 hours late)"
            print("MISSED", reel["title"])
            failed = True
            continue
        try:
            reel.update(post(reel, base, user), status="posted", note="", posted_at=now.isoformat())
            print("POSTED", reel["title"], reel.get("permalink", ""))
        except RuntimeError as e:
            reel["attempts"] = reel.get("attempts", 0) + 1
            reel["note"] = str(e)[:400]
            if reel["attempts"] >= ATTEMPTS:
                reel["status"] = "failed"
            print("FAILED", reel["title"], f"(attempt {reel['attempts']}):", reel["note"])
            failed = True
        QUEUE.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n")
    QUEUE.write_text(json.dumps(queue, indent=2, ensure_ascii=False) + "\n")
    return 1 if failed else 0  # a failed run makes GitHub email the owner


if __name__ == "__main__":
    sys.exit(main())
