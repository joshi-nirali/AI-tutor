"""Mint LiveKit access tokens + serve curriculum JSON and lesson images for the kid tutor UI.

Run alongside the React frontend (default port 5000).

Requires in .env:
  LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_URL (wss://...)

Optional:
  TOKEN_SERVER_PUBLIC_URL — full origin if the browser cannot use request host (e.g. https://api.example.com)
"""

from pathlib import Path

from curriculum import items_for_topic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from livekit.api import AccessToken, VideoGrants
import os
import re
import uvicorn

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

CURRICULUM_ROOT = Path(__file__).resolve().parent / "data" / "curriculum_images"
IMAGE_EXT = (".svg", ".png", ".webp", ".jpeg", ".jpg")
_TOPIC_SAFE = re.compile(r"^[a-z0-9_]+$")

# kidtutor-{mode}-{topic}-{tutor_slug}-{session_id}
_SAFE_ROOM = re.compile(
    r"^kidtutor-(vocabulary|speaking|quiz)-[a-z0-9_]+-[a-z][a-z0-9_]{0,14}-[a-zA-Z0-9]+$"
)


def _public_base(request: Request) -> str:
    env = os.getenv("TOKEN_SERVER_PUBLIC_URL", "").strip().rstrip("/")
    if env:
        return env
    return str(request.base_url).rstrip("/")


def _safe_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def resolve_curriculum_media_relpath(topic: str, word: str, explicit: str | None) -> str | None:
    root = CURRICULUM_ROOT.resolve()
    if explicit:
        p = (CURRICULUM_ROOT / topic / explicit).resolve()
        if p.is_file() and _safe_under(CURRICULUM_ROOT.resolve(), p):
            return p.relative_to(CURRICULUM_ROOT.resolve()).as_posix()
    slug = "".join(c if c.isalnum() or c in "-_." else "_" for c in word.lower()).strip("_")
    for ext in IMAGE_EXT:
        p = (CURRICULUM_ROOT / topic / f"{slug}{ext}").resolve()
        if p.is_file() and _safe_under(CURRICULUM_ROOT.resolve(), p):
            return p.relative_to(CURRICULUM_ROOT.resolve()).as_posix()
    return None


@app.get("/curriculum/{topic_slug}")
def curriculum_json(topic_slug: str, request: Request):
    """Lesson words + absolute imageUrl for each item (or null if no file)."""
    slug = topic_slug.strip().lower()
    if not _TOPIC_SAFE.match(slug):
        raise HTTPException(400, detail="Invalid topic slug")
    raw_items = items_for_topic(slug)
    base = _public_base(request)
    out = []
    for it in raw_items:
        word = it["word"]
        explicit = it.get("image")
        rel = resolve_curriculum_media_relpath(slug, word, explicit)
        image_url = f"{base}/curriculum-media/{rel}" if rel else None
        row = {"word": word, "imageUrl": image_url}
        if it.get("caption"):
            row["caption"] = it["caption"]
        out.append(row)
    return {"topic": slug, "items": out}


@app.get("/token")
def get_token(
    room: str = Query(
        ...,
        min_length=8,
        max_length=160,
        description="kidtutor-{mode}-{topic}-{tutor}-{sessionId}",
    ),
    identity: str = Query("friend", min_length=1, max_length=64),
    name: str = Query("Friend", min_length=1, max_length=64),
):
    api_key = os.getenv("LIVEKIT_API_KEY")
    api_secret = os.getenv("LIVEKIT_API_SECRET")
    livekit_url = os.getenv("LIVEKIT_URL", "").strip()

    if not api_key or not api_secret:
        raise HTTPException(
            status_code=500,
            detail="Missing LIVEKIT_API_KEY or LIVEKIT_API_SECRET in .env",
        )
    if not livekit_url:
        raise HTTPException(status_code=500, detail="Missing LIVEKIT_URL in .env")

    room_lc = room.strip().lower()
    if not _SAFE_ROOM.match(room_lc):
        raise HTTPException(
            status_code=400,
            detail="Invalid room name. Expected kidtutor-{vocabulary|speaking|quiz}-{topic}-{tutor}-{id}",
        )

    token = AccessToken(api_key, api_secret)
    token = token.with_identity(identity[:64]).with_name(name[:64])
    token = token.with_grants(
        VideoGrants(
            room_join=True,
            room=room,
            can_publish=True,
            can_subscribe=True,
        )
    )

    return {
        "token": token.to_jwt(),
        "url": livekit_url,
    }


@app.get("/health")
def health():
    """Use this to confirm the running server matches the app (room name includes tutor slug)."""
    return {
        "ok": True,
        "kid_room_format": "kidtutor-{vocabulary|speaking|quiz}-{topic}-{tutor}-{sessionId}",
    }


if CURRICULUM_ROOT.is_dir():
    app.mount(
        "/curriculum-media",
        StaticFiles(directory=str(CURRICULUM_ROOT)),
        name="curriculum_media",
    )


if __name__ == "__main__":
    print("Token server — set LIVEKIT_* in essence-cloud/.env")
    print("Room names must look like: kidtutor-vocabulary-animals-leo-a1b2c3d4 (includes tutor slug)")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("TOKEN_SERVER_PORT", "5000")))
