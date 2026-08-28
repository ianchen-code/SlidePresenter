import base64
import hashlib
import json
import mimetypes
import os
import secrets
import shutil
import threading
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional
from io import BytesIO

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

import psycopg
from psycopg.rows import dict_row
import boto3
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth

from pipeline import (
    run_pipeline,
    regenerate_slides,
    generate_title_description,
    improve_title_description,
    edit_slide_narration,
    get_slide_narration,
)

# ---------------------------------------------------------------------------
# Configuration from Environment Variables
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://slidepresenter:slidepresenter@localhost:5432/slidepresenter")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "slidepresenter-files")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")  # For MinIO: http://localhost:9000

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
APP_SECRET_KEY = os.environ.get("APP_SECRET_KEY", secrets.token_hex(32))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data", "jobs")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
SECRET_KEY_PATH = os.path.join(BASE_DIR, "data", ".secret_key")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

app = FastAPI(title="Slide-to-Video Pipeline")
app.add_middleware(SessionMiddleware, secret_key=APP_SECRET_KEY)

# ---------------------------------------------------------------------------
# OAuth Setup
# ---------------------------------------------------------------------------

oauth = OAuth()
oauth.register(
    name='google',
    client_id=GOOGLE_CLIENT_ID,
    client_secret=GOOGLE_CLIENT_SECRET,
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)

# ---------------------------------------------------------------------------
# S3 Client Setup
# ---------------------------------------------------------------------------

def get_s3_client():
    """Get S3 client with configured credentials."""
    kwargs = {
        'region_name': AWS_REGION,
    }

    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs['aws_access_key_id'] = AWS_ACCESS_KEY_ID
        kwargs['aws_secret_access_key'] = AWS_SECRET_ACCESS_KEY

    # For MinIO or other S3-compatible services
    if S3_ENDPOINT_URL:
        kwargs['endpoint_url'] = S3_ENDPOINT_URL

    return boto3.client('s3', **kwargs)

def upload_to_s3(local_path: str, s3_key: str) -> str:
    """Upload a file to S3 and return the S3 key. boto3's upload_file()
    doesn't set Content-Type on its own -- without it S3/MinIO serves
    everything as binary/octet-stream, which browsers refuse to play
    inline in a <video> tag even though the bytes are a valid MP4."""
    content_type, _ = mimetypes.guess_type(local_path)
    s3 = get_s3_client()
    extra_args = {"ContentType": content_type} if content_type else {}
    s3.upload_file(local_path, S3_BUCKET, s3_key, ExtraArgs=extra_args)
    return s3_key

def download_from_s3(s3_key: str, local_path: str):
    """Download a file from S3 to local path."""
    s3 = get_s3_client()
    s3.download_file(S3_BUCKET, s3_key, local_path)

def get_s3_presigned_url(s3_key: str, expires_in: int = 3600) -> str:
    """Generate a presigned URL for S3 object."""
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': S3_BUCKET, 'Key': s3_key},
        ExpiresIn=expires_in,
    )

def stream_from_s3(s3_key: str):
    """Stream a file from S3."""
    s3 = get_s3_client()
    response = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    return response['Body']

def delete_from_s3(s3_key: str):
    """Delete a file from S3."""
    s3 = get_s3_client()
    try:
        s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)
    except ClientError:
        pass

# ---------------------------------------------------------------------------
# Encryption for Token Storage
# ---------------------------------------------------------------------------

def get_or_create_secret_key() -> bytes:
    """Get or create a secret key for token encryption."""
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "rb") as f:
            return f.read()
    else:
        key = secrets.token_bytes(32)
        with open(SECRET_KEY_PATH, "wb") as f:
            f.write(key)
        os.chmod(SECRET_KEY_PATH, 0o600)
        return key

SECRET_KEY = get_or_create_secret_key()
# Fernet (AES-128-CBC + HMAC-SHA256) needs a 32-byte urlsafe-base64 key.
_fernet = Fernet(base64.urlsafe_b64encode(SECRET_KEY))

def encrypt_token(token: str) -> str:
    """Encrypt a token for storage."""
    return _fernet.encrypt(token.encode('utf-8')).decode('utf-8')

def decrypt_token(encrypted: str) -> str:
    """Decrypt a token. Raises InvalidToken if it was encrypted with a
    different key (e.g. a token saved before the encryption scheme changed)."""
    return _fernet.decrypt(encrypted.encode('utf-8')).decode('utf-8')

def mask_token(token: str) -> str:
    """Mask a token for display, showing only first 7 and last 4 characters."""
    if len(token) <= 11:
        return "*" * len(token)
    return token[:7] + "..." + token[-4:]

# ---------------------------------------------------------------------------
# PostgreSQL Database Setup
# ---------------------------------------------------------------------------

def get_db_connection():
    """Get a PostgreSQL database connection."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)

def init_db():
    """Initialize the PostgreSQL database with required tables."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            picture TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            filename TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'queued',
            stage TEXT NOT NULL DEFAULT 'queued',
            current_slide INTEGER DEFAULT 0,
            total_slides INTEGER DEFAULT 0,
            message TEXT DEFAULT 'Waiting to start',
            error TEXT,
            video_ready BOOLEAN DEFAULT FALSE,
            video_s3_key TEXT,
            original_file_s3_key TEXT,
            api_key_hash TEXT NOT NULL,
            voice TEXT NOT NULL,
            model TEXT NOT NULL
        )
    """)

    # Added after the initial release: rename/description and link-sharing.
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS title TEXT")
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS description TEXT")
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS share_token TEXT")
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS share_enabled BOOLEAN NOT NULL DEFAULT FALSE")
    cursor.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS share_permission TEXT NOT NULL DEFAULT 'view'")
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS jobs_share_token_idx
        ON jobs(share_token) WHERE share_token IS NOT NULL
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            id TEXT PRIMARY KEY,
            user_id TEXT REFERENCES users(id),
            name TEXT NOT NULL,
            provider TEXT NOT NULL DEFAULT 'anthropic',
            token_encrypted TEXT NOT NULL,
            token_masked TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_used_at TIMESTAMP
        )
    """)
    cursor.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE")

    conn.commit()
    cursor.close()
    conn.close()

# Initialize database on startup
try:
    init_db()
except Exception as e:
    print(f"Warning: Could not initialize database: {e}")

def hash_api_key(api_key: str) -> str:
    """Hash the API key using SHA-256."""
    return hashlib.sha256(api_key.encode()).hexdigest()

DB_LOCK = threading.Lock()

@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = get_db_connection()
    try:
        yield conn
    finally:
        conn.close()

def job_row_to_dict(row: dict) -> dict:
    """Convert a database row to a dictionary."""
    return {
        "id": row["id"],
        "user_id": row.get("user_id"),
        "filename": row["filename"],
        "title": row.get("title"),
        "description": row.get("description"),
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "status": row["status"],
        "stage": row["stage"],
        "current_slide": row["current_slide"],
        "total_slides": row["total_slides"],
        "message": row["message"],
        "error": row["error"],
        "video_ready": row["video_ready"],
        "video_s3_key": row.get("video_s3_key"),
        "original_file_s3_key": row.get("original_file_s3_key"),
        "voice": row["voice"],
        "model": row["model"],
        "share_enabled": row.get("share_enabled", False),
        "share_permission": row.get("share_permission", "view"),
        "share_token": row.get("share_token"),
    }

# ---------------------------------------------------------------------------
# Authentication Helpers
# ---------------------------------------------------------------------------

def get_current_user(request: Request) -> Optional[dict]:
    """Get the current logged-in user from session."""
    return request.session.get("user")

def require_auth(request: Request) -> dict:
    """Require authentication, raise 401 if not logged in."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user

def caller_id(request: Request) -> Optional[str]:
    """The current caller's user id, or None for a guest/anonymous caller."""
    user = get_current_user(request)
    return user["id"] if user else None

def _is_owner(job: dict, request: Request) -> bool:
    return job.get("user_id") == caller_id(request)

def _share_link_matches(job: dict, share_token: Optional[str]) -> bool:
    return bool(share_token) and bool(job.get("share_enabled")) and share_token == job.get("share_token")

def check_job_owner(job: dict, request: Request):
    """Strict: owner (or guest bucket) only. A share link never satisfies
    this -- used for delete and for changing share settings."""
    if not _is_owner(job, request):
        raise HTTPException(403, "Not authorized to access this job")

def check_job_view_access(job: dict, request: Request, share_token: Optional[str] = None):
    """Owner, or a valid share link (view or edit permission both grant viewing)."""
    if _is_owner(job, request) or _share_link_matches(job, share_token):
        return
    raise HTTPException(403, "Not authorized to access this job")

def check_job_edit_access(job: dict, request: Request, share_token: Optional[str] = None):
    """Owner, or a valid share link with edit permission specifically."""
    if _is_owner(job, request):
        return
    if _share_link_matches(job, share_token) and job.get("share_permission") == "edit":
        return
    raise HTTPException(403, "Not authorized to edit this job")

INTERNAL_JOB_FIELDS = ("video_s3_key", "original_file_s3_key", "api_key_hash")

def public_job_dict(job: dict, request: Request) -> dict:
    """Job fields safe to return to the caller. share_token is only included
    for the owner -- a share-link visitor already has the token in their URL,
    but shouldn't be handed a token they weren't explicitly given."""
    exclude = set(INTERNAL_JOB_FIELDS)
    if not _is_owner(job, request):
        exclude.add("share_token")
    return {k: v for k, v in job.items() if k not in exclude}

# ---------------------------------------------------------------------------
# Database CRUD Operations
# ---------------------------------------------------------------------------

def create_job_record(
    job_id: str,
    filename: str,
    api_key: str,
    voice: str,
    model: str,
    user_id: Optional[str] = None,
    original_file_s3_key: Optional[str] = None,
) -> dict:
    """Create a new job record in the database."""
    api_key_hash = hash_api_key(api_key)

    with DB_LOCK:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO jobs (id, user_id, filename, api_key_hash, voice, model, original_file_s3_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING created_at
            """, (job_id, user_id, filename, api_key_hash, voice, model, original_file_s3_key))
            result = cursor.fetchone()
            conn.commit()

    return {
        "id": job_id,
        "user_id": user_id,
        "filename": filename,
        "created_at": result["created_at"].isoformat() if result else None,
        "status": "queued",
        "stage": "queued",
        "current_slide": 0,
        "total_slides": 0,
        "message": "Waiting to start",
        "error": None,
        "video_ready": False,
    }

def update_job(job_id: str, **fields):
    """Update a job record in the database."""
    if not fields:
        return

    set_clause = ", ".join(f"{k} = %s" for k in fields.keys())
    values = list(fields.values()) + [job_id]

    with DB_LOCK:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE jobs SET {set_clause} WHERE id = %s", values)
            conn.commit()

def get_job_by_id(job_id: str) -> Optional[dict]:
    """Get a job by ID."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        row = cursor.fetchone()
        if row:
            return job_row_to_dict(row)
    return None

def get_all_jobs(user_id: Optional[str] = None) -> List[dict]:
    """Get jobs owned by user_id, or guest (ownerless) jobs if user_id is None."""
    with get_db() as conn:
        cursor = conn.cursor()
        if user_id:
            cursor.execute("SELECT * FROM jobs WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        else:
            cursor.execute("SELECT * FROM jobs WHERE user_id IS NULL ORDER BY created_at DESC")
        rows = cursor.fetchall()
        return [job_row_to_dict(row) for row in rows]

# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------

@app.get("/auth/login")
async def login(request: Request):
    """Initiate Google OAuth login."""
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(500, "OAuth not configured")
    redirect_uri = request.url_for('auth_callback')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.get("/auth/callback")
async def auth_callback(request: Request):
    """Handle Google OAuth callback."""
    try:
        token = await oauth.google.authorize_access_token(request)
        print(f"Token received: {token}")

        # authorize_access_token() already verifies the id_token's signature
        # and populates userinfo for OIDC-registered clients. If it's ever
        # missing, fall back to a live call to Google's userinfo endpoint
        # rather than locally decoding the id_token unverified -- that would
        # let a forged/tampered token impersonate any user.
        user_info = token.get('userinfo')
        if not user_info:
            user_info = await oauth.google.userinfo(token=token)

        print(f"User info: {user_info}")

        if not user_info:
            raise HTTPException(400, "Failed to get user info from token")

        user_id = user_info.get('sub')
        email = user_info.get('email')
        name = user_info.get('name', '')
        picture = user_info.get('picture', '')

        if not user_id or not email:
            raise HTTPException(400, f"Missing user_id or email in user_info: {user_info}")

        # Create or update user in database
        with DB_LOCK:
            with get_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO users (id, email, name, picture, last_login_at)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (id) DO UPDATE SET
                        email = EXCLUDED.email,
                        name = EXCLUDED.name,
                        picture = EXCLUDED.picture,
                        last_login_at = CURRENT_TIMESTAMP
                """, (user_id, email, name, picture))
                conn.commit()

        # Store user in session
        request.session["user"] = {
            "id": user_id,
            "email": email,
            "name": name,
            "picture": picture,
        }

        return RedirectResponse(url="/", status_code=302)
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(400, f"Authentication failed: {type(e).__name__}: {str(e)}")

@app.get("/auth/logout")
async def logout(request: Request):
    """Log out the current user."""
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)

@app.get("/api/auth/me")
async def get_me(request: Request):
    """Get current user info."""
    user = get_current_user(request)
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, "user": user}

# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/jobs")
async def create_job(
    request: Request,
    file: UploadFile = File(...),
    api_key: str = Form(...),
    voice: str = Form("en-US-ChristopherNeural"),
    model: str = Form(...),
):
    if not file.filename.lower().endswith((".pdf", ".pptx")):
        raise HTTPException(400, "File must be a .pdf or .pptx")

    user = get_current_user(request)
    user_id = user["id"] if user else None

    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DATA_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Save file locally first
    upload_path = os.path.join(job_dir, file.filename)
    with open(upload_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Upload to S3
    original_s3_key = f"jobs/{job_id}/original/{file.filename}"
    try:
        upload_to_s3(upload_path, original_s3_key)
    except Exception as e:
        # S3 upload failed, continue with local storage
        print(f"S3 upload failed: {e}, using local storage")
        original_s3_key = None

    # Create job record in database
    create_job_record(
        job_id, file.filename, api_key, voice, model,
        user_id=user_id,
        original_file_s3_key=original_s3_key,
    )

    # Start background thread
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, upload_path, job_dir, api_key, voice, model),
        daemon=True,
    )
    thread.start()

    return {"job_id": job_id}


def _run_job(job_id, upload_path, job_dir, api_key, voice, model):
    def progress_cb(stage, cur, total, msg):
        update_job(
            job_id,
            status="running",
            stage=stage,
            current_slide=cur or 0,
            total_slides=total or 0,
            message=msg or stage,
        )

    try:
        update_job(job_id, status="running", stage="starting", message="Starting pipeline")
        result = run_pipeline(
            input_path=upload_path,
            job_dir=job_dir,
            api_key=api_key,
            voice=voice,
            model=model,
            progress_cb=progress_cb,
        )
        final_path = result["video_path"]

        # Upload video to S3
        video_s3_key = f"jobs/{job_id}/video/final_presentation.mp4"
        try:
            upload_to_s3(final_path, video_s3_key)
        except Exception as e:
            print(f"S3 upload failed: {e}, video available locally")
            video_s3_key = None

        # AI-suggested title/description, if that step succeeded and nothing
        # else set them in the meantime (e.g. a very fast manual rename).
        job_now = get_job_by_id(job_id)
        extra_fields = {}
        if result.get("title") and not (job_now and job_now.get("title")):
            extra_fields["title"] = result["title"]
        if result.get("description") and not (job_now and job_now.get("description")):
            extra_fields["description"] = result["description"]

        update_job(
            job_id,
            status="done",
            stage="done",
            message="Video ready",
            video_ready=True,
            video_s3_key=video_s3_key,
            **extra_fields,
        )
    except Exception as e:
        traceback.print_exc()
        update_job(job_id, status="error", stage="error", error=str(e))


@app.get("/api/jobs")
async def list_jobs(request: Request):
    jobs = get_all_jobs(user_id=caller_id(request))
    return [public_job_dict(job, request) for job in jobs]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, request: Request, share: Optional[str] = None):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_view_access(job, request, share)
    result = public_job_dict(job, request)
    result["is_owner"] = _is_owner(job, request)
    result["can_edit"] = result["is_owner"] or (
        _share_link_matches(job, share) and job.get("share_permission") == "edit"
    )
    return result


class JobDetailsUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None

@app.put("/api/jobs/{job_id}/details")
async def update_job_details(job_id: str, data: JobDetailsUpdate, request: Request, share: Optional[str] = None):
    """Rename / set description. Owner or an 'edit' share link."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_edit_access(job, request, share)

    updates = {}
    if data.title is not None:
        updates["title"] = data.title.strip() or None
    if data.description is not None:
        updates["description"] = data.description.strip() or None
    if updates:
        update_job(job_id, **updates)

    return {"status": "updated"}


class GenerateDetailsRequest(BaseModel):
    api_key: str
    action: str = "generate"  # "generate" | "improve"
    current_title: Optional[str] = None
    current_description: Optional[str] = None

@app.post("/api/jobs/{job_id}/generate-details")
async def generate_job_details(job_id: str, data: GenerateDetailsRequest, request: Request, share: Optional[str] = None):
    """AI-suggest a title + description from the transcript, or improve the
    caller's current draft of them. Returns the suggestion without saving it
    -- the caller (Edit Details modal) still has to Save. Owner or an 'edit'
    share link."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_edit_access(job, request, share)

    if data.action not in ("generate", "improve"):
        raise HTTPException(400, "action must be 'generate' or 'improve'")

    transcript_path = os.path.join(DATA_DIR, job_id, "transcript.json")
    if not os.path.exists(transcript_path):
        raise HTTPException(409, "Transcript not ready yet")
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    model = job.get("model") or "claude-sonnet-4-5"
    try:
        if data.action == "improve" and (data.current_title or data.current_description):
            return improve_title_description(data.current_title, data.current_description, transcript, data.api_key, model=model)
        return generate_title_description(transcript, data.api_key, model=model)
    except Exception as e:
        raise HTTPException(502, f"AI auto-fill failed: {e}")


class ShareSettings(BaseModel):
    enabled: bool
    permission: str = "view"  # "view" or "edit"

@app.post("/api/jobs/{job_id}/share")
async def update_job_share(job_id: str, data: ShareSettings, request: Request):
    """Turn link-sharing on/off and choose view vs edit. Owner only --
    a share link itself can never grant the ability to change sharing."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_owner(job, request)

    if data.permission not in ("view", "edit"):
        raise HTTPException(400, "permission must be 'view' or 'edit'")

    token = job.get("share_token")
    updates = {"share_enabled": data.enabled, "share_permission": data.permission}
    if data.enabled and not token:
        token = secrets.token_urlsafe(24)
        updates["share_token"] = token
    update_job(job_id, **updates)

    return {"share_enabled": data.enabled, "share_permission": data.permission, "share_token": token}


@app.get("/api/jobs/{job_id}/video")
async def get_job_video(job_id: str, request: Request, share: Optional[str] = None):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_view_access(job, request, share)
    if not job.get("video_ready"):
        raise HTTPException(409, "Video not ready yet")

    # Try S3 first
    video_s3_key = job.get("video_s3_key")
    if video_s3_key:
        try:
            presigned_url = get_s3_presigned_url(video_s3_key)
            return RedirectResponse(url=presigned_url, status_code=302)
        except Exception as e:
            print(f"S3 presigned URL failed: {e}")

    # Fallback to local file
    local_path = os.path.join(DATA_DIR, job_id, "final_presentation.mp4")
    if os.path.exists(local_path):
        return FileResponse(local_path, media_type="video/mp4", filename="presentation.mp4")

    raise HTTPException(404, "Video file not found")


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    """Delete a job and its associated files."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_owner(job, request)

    # Delete from S3
    if job.get("video_s3_key"):
        try:
            delete_from_s3(job["video_s3_key"])
        except Exception as e:
            print(f"Failed to delete video from S3: {e}")

    if job.get("original_file_s3_key"):
        try:
            delete_from_s3(job["original_file_s3_key"])
        except Exception as e:
            print(f"Failed to delete original file from S3: {e}")

    # Delete local files
    job_dir = os.path.join(DATA_DIR, job_id)
    if os.path.exists(job_dir):
        shutil.rmtree(job_dir)

    # Delete from database
    with DB_LOCK:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM jobs WHERE id = %s", (job_id,))
            conn.commit()

    return {"status": "deleted"}


@app.get("/api/jobs/{job_id}/transcript")
async def get_job_transcript(job_id: str, request: Request, share: Optional[str] = None):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_view_access(job, request, share)

    transcript_path = os.path.join(DATA_DIR, job_id, "transcript.json")
    if not os.path.exists(transcript_path):
        raise HTTPException(409, "Transcript not ready yet")

    with open(transcript_path, "r", encoding="utf-8") as f:
        return json.load(f)


class SlideAiTextRequest(BaseModel):
    api_key: str
    action: str  # "regenerate" | "improve" | "custom"
    current_text: Optional[str] = None  # required for improve/custom
    prompt: Optional[str] = None  # required for custom

@app.post("/api/jobs/{job_id}/slides/{slide_num}/ai-text")
async def slide_ai_text(job_id: str, slide_num: int, data: SlideAiTextRequest, request: Request, share: Optional[str] = None):
    """AI actions on one slide's narration text, from the per-slide spark
    menu in the transcript editor. Returns the new text without saving it --
    the caller applies it to the textarea, which is only persisted when the
    user hits Regenerate. Owner or an 'edit' share link."""
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_edit_access(job, request, share)

    model = job.get("model") or "claude-sonnet-4-5"

    if data.action == "regenerate":
        image_path = os.path.join(DATA_DIR, job_id, "slides", f"slide_{slide_num:02d}.png")
        if not os.path.exists(image_path):
            raise HTTPException(404, "Slide image not found")
        try:
            return {"text": get_slide_narration(image_path, data.api_key, model=model)}
        except Exception as e:
            raise HTTPException(502, f"AI regeneration failed: {e}")

    if data.action in ("improve", "custom"):
        if not data.current_text:
            raise HTTPException(400, "current_text is required")
        if data.action == "custom" and not (data.prompt or "").strip():
            raise HTTPException(400, "prompt is required for a custom edit")
        try:
            text = edit_slide_narration(
                data.current_text,
                data.api_key,
                model=model,
                instruction=data.prompt if data.action == "custom" else None,
            )
            return {"text": text}
        except Exception as e:
            raise HTTPException(502, f"AI edit failed: {e}")

    raise HTTPException(400, "action must be 'regenerate', 'improve', or 'custom'")


# ---------------------------------------------------------------------------
# Regenerate Slides Endpoint
# ---------------------------------------------------------------------------

class SlideChange(BaseModel):
    slide: int
    text: str

class RegenerateRequest(BaseModel):
    changes: List[SlideChange]
    api_key: str

@app.post("/api/jobs/{job_id}/regenerate")
async def regenerate_job_slides(job_id: str, body: RegenerateRequest, request: Request, share: Optional[str] = None):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    check_job_edit_access(job, request, share)
    if not job.get("video_ready"):
        raise HTTPException(409, "Cannot regenerate: original video not ready")

    job_dir = os.path.join(DATA_DIR, job_id)

    # Update status
    update_job(job_id, status="running", stage="regenerating", message="Regenerating slides...")

    # Start background thread for regeneration
    thread = threading.Thread(
        target=_regenerate_job,
        args=(job_id, job_dir, body.changes, body.api_key, job["voice"]),
        daemon=True,
    )
    thread.start()

    return {"status": "regenerating", "slides": [c.slide for c in body.changes]}


def _regenerate_job(job_id: str, job_dir: str, changes: List[SlideChange], api_key: str, voice: str):
    def progress_cb(stage, cur, total, msg):
        update_job(
            job_id,
            status="running",
            stage=stage,
            current_slide=cur or 0,
            total_slides=total or 0,
            message=msg or stage,
        )

    try:
        changes_dict = {c.slide: c.text for c in changes}
        final_path = regenerate_slides(
            job_dir=job_dir,
            changes=changes_dict,
            api_key=api_key,
            voice=voice,
            progress_cb=progress_cb,
        )

        # Upload video to S3
        video_s3_key = f"jobs/{job_id}/video/final_presentation.mp4"
        try:
            upload_to_s3(final_path, video_s3_key)
        except Exception as e:
            print(f"S3 upload failed: {e}")
            video_s3_key = None

        update_job(
            job_id,
            status="done",
            stage="done",
            message="Video ready",
            video_ready=True,
            video_s3_key=video_s3_key,
        )
    except Exception as e:
        traceback.print_exc()
        update_job(job_id, status="error", stage="error", error=str(e))


# ---------------------------------------------------------------------------
# Token Management Endpoints
# ---------------------------------------------------------------------------

class TokenCreate(BaseModel):
    name: str
    token: str
    provider: str = "anthropic"
    is_default: bool = False

class TokenUpdate(BaseModel):
    name: Optional[str] = None
    token: Optional[str] = None
    provider: Optional[str] = None
    is_default: Optional[bool] = None

def _token_dict(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "token_masked": row["token_masked"],
        "is_default": row["is_default"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
    }

@app.get("/api/tokens")
async def list_tokens(request: Request):
    """List all saved tokens (masked)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, provider, token_masked, is_default, created_at, last_used_at
            FROM tokens WHERE user_id IS NOT DISTINCT FROM %s ORDER BY created_at DESC
        """, (caller_id(request),))
        return [_token_dict(row) for row in cursor.fetchall()]

# Registered before /api/tokens/{token_id} so "default" isn't captured as an id.
@app.get("/api/tokens/default")
async def get_default_token(request: Request):
    """The caller's default token (masked), or null if none is set."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, provider, token_masked, is_default, created_at, last_used_at
            FROM tokens WHERE user_id IS NOT DISTINCT FROM %s AND is_default = TRUE
        """, (caller_id(request),))
        row = cursor.fetchone()
        return _token_dict(row) if row else None

@app.get("/api/tokens/default/decrypt")
async def get_default_token_decrypted(request: Request):
    """The caller's default token, decrypted -- used by every AI action in
    the app so no page needs its own token picker."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, token_encrypted FROM tokens
            WHERE user_id IS NOT DISTINCT FROM %s AND is_default = TRUE
        """, (caller_id(request),))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "No default API token set. Add one in Manage Tokens.")

        with DB_LOCK:
            cursor.execute("UPDATE tokens SET last_used_at = CURRENT_TIMESTAMP WHERE id = %s", (row["id"],))
            conn.commit()

        try:
            return {"token": decrypt_token(row["token_encrypted"])}
        except InvalidToken:
            raise HTTPException(400, "Your default token was saved with a previous encryption scheme. Please delete and re-add it.")

@app.post("/api/tokens")
async def create_token(request: Request, data: TokenCreate):
    """Create a new token. The first token a user ever saves automatically
    becomes their default; later ones only become default if requested."""
    user_id = caller_id(request)
    token_id = str(uuid.uuid4())
    token_encrypted = encrypt_token(data.token)
    token_masked = mask_token(data.token)

    with DB_LOCK:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS c FROM tokens WHERE user_id IS NOT DISTINCT FROM %s", (user_id,))
            is_first = cursor.fetchone()["c"] == 0
            make_default = data.is_default or is_first

            if make_default:
                cursor.execute("UPDATE tokens SET is_default = FALSE WHERE user_id IS NOT DISTINCT FROM %s", (user_id,))

            cursor.execute("""
                INSERT INTO tokens (id, user_id, name, provider, token_encrypted, token_masked, is_default)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING created_at
            """, (token_id, user_id, data.name, data.provider, token_encrypted, token_masked, make_default))
            result = cursor.fetchone()
            conn.commit()

    return {
        "id": token_id,
        "name": data.name,
        "provider": data.provider,
        "token_masked": token_masked,
        "is_default": make_default,
        "created_at": result["created_at"].isoformat() if result else None,
    }

@app.get("/api/tokens/{token_id}")
async def get_token(token_id: str, request: Request):
    """Get a token by ID (masked). Scoped to the caller, same as the list endpoint."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, provider, token_masked, is_default, created_at, last_used_at
            FROM tokens WHERE id = %s AND user_id IS NOT DISTINCT FROM %s
        """, (token_id, caller_id(request)))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "Token not found")
        return _token_dict(row)

@app.get("/api/tokens/{token_id}/decrypt")
async def get_decrypted_token(token_id: str, request: Request):
    """Get the decrypted token value (for internal use). Scoped to the caller."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT token_encrypted FROM tokens WHERE id = %s AND user_id IS NOT DISTINCT FROM %s",
            (token_id, caller_id(request)),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "Token not found")

        # Update last_used_at
        with DB_LOCK:
            cursor.execute(
                "UPDATE tokens SET last_used_at = CURRENT_TIMESTAMP WHERE id = %s",
                (token_id,)
            )
            conn.commit()

        try:
            return {"token": decrypt_token(row["token_encrypted"])}
        except InvalidToken:
            raise HTTPException(400, "This token was saved with a previous encryption scheme. Please delete and re-add it.")

@app.put("/api/tokens/{token_id}")
async def update_token(token_id: str, data: TokenUpdate, request: Request):
    """Update a token. Scoped to the caller. Setting is_default=true demotes
    whichever other token was previously the default."""
    user_id = caller_id(request)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM tokens WHERE id = %s AND user_id IS NOT DISTINCT FROM %s",
            (token_id, user_id),
        )
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "Token not found")

        updates = {}
        if data.name is not None:
            updates["name"] = data.name
        if data.provider is not None:
            updates["provider"] = data.provider
        if data.token is not None:
            updates["token_encrypted"] = encrypt_token(data.token)
            updates["token_masked"] = mask_token(data.token)
        if data.is_default is not None:
            updates["is_default"] = data.is_default

        with DB_LOCK:
            if data.is_default is True:
                cursor.execute(
                    "UPDATE tokens SET is_default = FALSE WHERE user_id IS NOT DISTINCT FROM %s AND id != %s",
                    (user_id, token_id),
                )
            if updates:
                set_clause = ", ".join(f"{k} = %s" for k in updates.keys())
                values = list(updates.values()) + [token_id]
                cursor.execute(f"UPDATE tokens SET {set_clause} WHERE id = %s", values)
            conn.commit()

    return {"status": "updated"}

@app.delete("/api/tokens/{token_id}")
async def delete_token(token_id: str, request: Request):
    """Delete a token. Scoped to the caller. If it was the default, promotes
    the most recently created remaining token so a default always exists."""
    user_id = caller_id(request)
    with DB_LOCK:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM tokens WHERE id = %s AND user_id IS NOT DISTINCT FROM %s RETURNING is_default",
                (token_id, user_id),
            )
            deleted = cursor.fetchone()
            if not deleted:
                raise HTTPException(404, "Token not found")

            if deleted["is_default"]:
                cursor.execute("""
                    UPDATE tokens SET is_default = TRUE WHERE id = (
                        SELECT id FROM tokens WHERE user_id IS NOT DISTINCT FROM %s
                        ORDER BY created_at DESC LIMIT 1
                    )
                """, (user_id,))
            conn.commit()
    return {"status": "deleted"}


# ---------------------------------------------------------------------------
# Frontend Routes
# ---------------------------------------------------------------------------

@app.get("/slide/{job_id}")
async def get_slide_page(job_id: str):
    slide_page = os.path.join(FRONTEND_DIR, "slide.html")
    return FileResponse(slide_page, media_type="text/html")

@app.get("/token")
async def get_token_page():
    token_page = os.path.join(FRONTEND_DIR, "token.html")
    return FileResponse(token_page, media_type="text/html")

@app.get("/login")
async def get_login_page():
    login_page = os.path.join(FRONTEND_DIR, "login.html")
    return FileResponse(login_page, media_type="text/html")


# ---------------------------------------------------------------------------
# Error Pages
# ---------------------------------------------------------------------------

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 404:
        return FileResponse(os.path.join(FRONTEND_DIR, "error-404.html"), status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    traceback.print_exc()
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return FileResponse(os.path.join(FRONTEND_DIR, "error-500.html"), status_code=500)


# Serve the simple frontend
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
