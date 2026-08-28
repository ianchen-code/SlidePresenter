# Slide → Narrated Video

Turns a `.pdf` or `.pptx` deck into a narrated video: each slide is rendered
to an image, narrated by Claude, converted to speech (edge-tts), turned into
a per-slide clip (ffmpeg), then concatenated into one final video.

## How it's structured

```
app/
  backend/
    main.py          FastAPI app: upload endpoint, job status, video download
    pipeline.py       the actual pipeline (same steps as your notebook)
    requirements.txt
  frontend/
    index.html        single-page upload UI, polls job status, plays result
  data/jobs/<job_id>/  per-job working directory (uploads, slide images,
                        narration text, audio, per-slide clips, final mp4)
```

### Why this shape

The pipeline can take a couple of minutes for a 20-slide deck (LLM call +
TTS + ffmpeg encode, per slide). A plain request/response endpoint would
time out and give the browser nothing to show in the meantime. So:

- `POST /api/jobs` uploads the file, immediately returns a `job_id`, and
  starts the pipeline in a **background thread**.
- The frontend **polls** `GET /api/jobs/{job_id}` every 1.5s for status
  (`queued` → `running` → `done`/`error`), including which slide it's on,
  and updates a progress bar.
- Once `status == "done"`, the frontend points a `<video>` tag and a
  download link at `GET /api/jobs/{job_id}/video`.

Jobs live in an in-memory dict (`JOBS` in `main.py`). That's fine for one
process running locally or on a single small server. If you need multiple
server processes/workers or persistence across restarts, swap that dict for
Redis or a database table (job id → status/progress row) — the endpoint
shapes don't need to change.

## System dependencies (must be installed on the machine, not via pip)

- **LibreOffice** (`soffice`) — converts `.pptx` → `.pdf`
- **poppler-utils** — backs `pdf2image` (`pdftoppm`)
- **ffmpeg** / **ffprobe** — builds and concatenates video clips

Ubuntu/Debian:
```bash
sudo apt-get update
sudo apt-get install -y libreoffice poppler-utils ffmpeg
```

macOS (Homebrew):
```bash
brew install libreoffice poppler ffmpeg
```

## Setup

```bash
cd app/backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
cd app/backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`. Upload a `.pdf`/`.pptx`, paste an Anthropic
API key (from console.anthropic.com — this is sent per-request, not stored
server-side), pick a voice, submit.

## Notes on the parts that changed from the notebook

- **Narration call**: the notebook hit a custom OpenAI-compatible proxy
  (`hnd1.aihub.zeabur.ai`) with model `"claude-sonnet-4-5"`. I switched
  `get_slide_narration` to call the **official Anthropic Messages API**
  (`api.anthropic.com/v1/messages`) directly with an image content block,
  since that's the stable, documented path — no third-party proxy
  dependency. If you specifically want to keep using your proxy, it's a
  small edit to `pipeline.py::get_slide_narration` (swap the URL/headers/
  response-parsing back to the OpenAI-style shape you had).
- **`await` at top level**: the notebook's cell 3 used a bare `await`
  (works in Jupyter, not in a normal Python script). `pipeline.py` wraps
  the TTS call in `asyncio.run(...)` instead, since `run_pipeline` is
  called from a background thread with no existing event loop.
- **Everything is per-job now**: `slides/`, narration `.txt`, `.mp3`, and
  per-slide `.mp4` files all live under `data/jobs/<job_id>/` instead of a
  shared `slides/` folder, so concurrent uploads from different users don't
  collide or overwrite each other.

## Things worth hardening before real deployment

- **API key handling**: currently the key is submitted per-request from
  the browser and only lives in memory for that job. Don't log it, and
  serve the site over HTTPS in production so it isn't sent in the clear.
- **Job cleanup**: nothing currently deletes old job directories. Add a
  periodic sweep (e.g. delete `data/jobs/*` older than N hours) or you'll
  fill up disk over time.
- **Concurrency limits**: right now any number of jobs can run in parallel
  threads, each shelling out to ffmpeg/soffice. Consider a bounded worker
  pool (e.g. `concurrent.futures.ThreadPoolExecutor(max_workers=2)`) if
  you expect concurrent users on modest hardware.
- **File size / page count limits**: add a max upload size and a max slide
  count so one huge deck can't tie up a worker for 20+ minutes.
- **Auth**: there's currently no login/rate-limiting — anyone who can reach
  the server can submit jobs (and burn your API credits, if you ever swap
  to a server-side key). Add basic auth or an API-key gate if this isn't
  just for local/personal use.
