# Essence Cloud — Kid Tutor

Voice lesson app for young children: **React** (browser) joins a **LiveKit** room, a **Python agent** runs **OpenAI Realtime** speech, and optionally **bitHuman** publishes the talking avatar video. A small **token server** mints room JWTs and serves curriculum images.

**How the full flow works (plain language):** see [WORKFLOW.md](WORKFLOW.md). **Flowchart (HTML):** open [WORKFLOW_DIAGRAM.html](WORKFLOW_DIAGRAM.html) in a browser.

---

## What you run locally


| Component              | Command                    | Purpose                                                    |
| ---------------------- | -------------------------- | ---------------------------------------------------------- |
| Token + curriculum API | `python token_server.py`   | `GET /token`, `GET /curriculum/...`, static lesson images  |
| LiveKit worker         | `python agent.py dev`      | Joins rooms, AI tutor, optional bitHuman avatar            |
| Web UI                 | `cd frontend && npm start` | Usually **[http://localhost:3000](http://localhost:3000)** |


Use **three terminals**. The UI shows a dev hint if `NODE_ENV=development`.

---

## Prerequisites

- **Python 3.10+** (3.12 recommended)
- **Node.js 18+** and npm
- **LiveKit Cloud** project ([cloud.livekit.io](https://cloud.livekit.io)) — WebSocket URL (`wss://...`) and API key + secret  
- **OpenAI API key** (Realtime / voice)
- **bitHuman** API secret + agent ID — *optional* for local testing (see [Voice-only mode](#voice-only-mode-no-bithuman))

---

## Run the application (step by step)

### 1. Install Python dependencies

From the repository root:

```bash
cd "/path/to/essence-cloud"
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure the root `.env`

```bash
cp .env.example .env
```

Edit `.env` and set at least:


| Variable              | Required        | Notes                                                       |
| --------------------- | --------------- | ----------------------------------------------------------- |
| `LIVEKIT_URL`         | Yes             | WebSocket URL from LiveKit Cloud (must start with `wss://`) |
| `LIVEKIT_API_KEY`     | Yes             | Project API key                                             |
| `LIVEKIT_API_SECRET`  | Yes             | Project API secret                                          |
| `OPENAI_API_KEY`      | Yes             | For `agent.py`                                              |
| `OPENAI_REALTIME_MODEL` | No          | Realtime voice model (default `gpt-realtime`). If logs show `model_not_found`, set a model your OpenAI org allows (e.g. `gpt-realtime-2025-08-28`). |
| `BITHUMAN_API_SECRET` | If using avatar | Omit or use voice-only mode below                           |
| `BITHUMAN_AGENT_ID`   | If using avatar | From [bithuman.ai](https://www.bithuman.ai)                 |


All optional knobs (prompt packs, scoring, auto-advance, etc.) are documented inline in `.env.example`.

### 3. Configure the React app

```bash
cp frontend/.env.example frontend/.env.local
```

- `**REACT_APP_TOKEN_SERVER_URL**` — must point at your token endpoint (default `http://127.0.0.1:5000/token` if `token_server` listens on port 5000).
- `**REACT_APP_LIVEKIT_URL**` — only needed if your `/token` response does not include a `url` field (the bundled `token_server.py` returns `url` from `LIVEKIT_URL`, so this is often optional).

### 4. Install frontend dependencies

```bash
cd frontend
npm install
cd ..
```

### 5. Start the token server

```bash
source .venv/bin/activate   # if not already active
pip install fastapi uvicorn python-multipart
python token_server.py
```

Listens on **port 5000** by default (`TOKEN_SERVER_PORT` to change).  
Quick check: open `http://127.0.0.1:5000/health` — you should see JSON with `ok: true`.

### 6. Start the LiveKit agent worker

In a **second** terminal (same repo root, venv active):

```bash
source .venv/bin/activate
python agent.py dev
```

Leave this running. It registers with LiveKit and handles rooms named like `kidtutor-{mode}-{topic}-{tutor}-{sessionId}`.

### 7. Start the React dev server

In a **third** terminal:

```bash
cd frontend
npm start
```

Open **[http://localhost:3000](http://localhost:3000)** in a browser (allow microphone when prompted).

### 8. Use the app

1. Enter the child’s name and pick a tutor (Leo / Luna).
2. Choose a **mode** (vocabulary, speaking, quiz) and a **lesson theme**.
3. Tap **Start** on the tutor screen so the client fetches a token and joins the room.
4. Ensure `**python agent.py dev`** is running — otherwise the child will see “hasn’t joined yet” after a few seconds.

---

## Voice-only mode (no bitHuman)

If you do not have `BITHUMAN_AGENT_ID` yet, set in `.env`:

```bash
KID_TUTOR_USE_AVATAR=0
```

Then `BITHUMAN_AGENT_ID` / `BITHUMAN_API_SECRET` are not required for the agent to run (audio-only tutor).

---

## Production build (frontend)

```bash
cd frontend
npm run build
```

Serve the `frontend/build` folder with any static host. Set `REACT_APP_*` at **build time** and ensure `TOKEN_SERVER_PUBLIC_URL` (or equivalent) is set on the token server if the browser calls the API from another origin.

---

## Project layout (main pieces)


| Path                   | Role                                                                                               |
| ---------------------- | -------------------------------------------------------------------------------------------------- |
| `agent.py`             | LiveKit agent: OpenAI Realtime, Silero VAD, optional bitHuman, lesson sync + pronunciation helpers |
| `token_server.py`      | FastAPI: LiveKit JWT + curriculum JSON + lesson images                                             |
| `frontend/`            | Create React App + LiveKit Components                                                              |
| `data/word_lists.json` | Per-topic vocabulary for lessons                                                                   |
| `data/prompts/*.json`  | Tutor prompts, pronunciation rules, response templates                                             |
| `curriculum.py`        | Loads word lists / items for the agent and token server                                            |


Legacy / extras:


| Path                 | Role                                                                                             |
| -------------------- | ------------------------------------------------------------------------------------------------ |
| `quickstart.py`      | Older local LiveKit + audio demo (not the same room as the kid tutor UI)                         |
| `docker-compose.yml` | Self-hosted LiveKit + different web UI image — not aligned with the CRA kid tutor in `frontend/` |


---

## Troubleshooting

**“Tutor hasn’t joined yet”**  
Run `python agent.py dev` with the **same** `LIVEKIT_*` values as `token_server.py` and `.env`.

**Token or CORS errors**  

- Token server must be reachable from the browser at `REACT_APP_TOKEN_SERVER_URL`.  
- For cross-origin production setups, configure CORS on `token_server.py` and set `TOKEN_SERVER_PUBLIC_URL` so curriculum image URLs are absolute and correct.

**Invalid room / 400 from `/token`**  
Room names must match: `kidtutor-{vocabulary|speaking|quiz}-{topic}-{tutor}-{id}`. The React app builds this automatically.

**No bitHuman video**  
Confirm `KID_TUTOR_USE_AVATAR` is not `0`, credentials are set, and the worker logs show `use_avatar=True`.

---

## License / upstream

This tree may include patterns from bitHuman / LiveKit examples; check repository or vendor docs for license details.