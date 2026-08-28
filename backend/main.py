import base64
import hashlib
import json
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
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Body, Request, Response
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from authlib.integrations.starlette_client import OAuth

from pipeline import run_pipeline, regenerate_slides

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
    """Upload a file to S3 and return the S3 key."""
    s3 = get_s3_client()
    s3.upload_file(local_path, S3_BUCKET, s3_key)
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

def encrypt_token(token: str) -> str:
    """Encrypt a token using XOR with the secret key and base64 encode."""
    token_bytes = token.encode('utf-8')
    key_extended = (SECRET_KEY * ((len(token_bytes) // len(SECRET_KEY)) + 1))[:len(token_bytes)]
    encrypted = bytes(a ^ b for a, b in zip(token_bytes, key_extended))
    return base64.b64encode(encrypted).decode('utf-8')

def decrypt_token(encrypted: str) -> str:
    """Decrypt a token."""
    encrypted_bytes = base64.b64decode(encrypted.encode('utf-8'))
    key_extended = (SECRET_KEY * ((len(encrypted_bytes) // len(SECRET_KEY)) + 1))[:len(encrypted_bytes)]
    decrypted = bytes(a ^ b for a, b in zip(encrypted_bytes, key_extended))
    return decrypted.decode('utf-8')

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
    """Get all jobs, optionally filtered by user, sorted by created_at descending."""
    with get_db() as conn:
        cursor = conn.cursor()
        if user_id:
            cursor.execute("SELECT * FROM jobs WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
        else:
            cursor.execute("SELECT * FROM jobs ORDER BY created_at DESC")
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

        # Try to get user info from token or fetch it
        user_info = token.get('userinfo')
        if not user_info and 'id_token' in token:
            # Parse the ID token
            from authlib.jose import jwt
            claims = jwt.decode(token['id_token'], claims_options={"verify_signature": False})
            user_info = dict(claims)

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
        final_path = run_pipeline(
            input_path=upload_path,
            job_dir=job_dir,
            api_key=api_key,
            voice=voice,
            model=model,
            progress_cb=progress_cb,
        )

        # Upload video to S3
        video_s3_key = f"jobs/{job_id}/video/final_presentation.mp4"
        try:
            upload_to_s3(final_path, video_s3_key)
        except Exception as e:
            print(f"S3 upload failed: {e}, video available locally")
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


@app.get("/api/jobs")
async def list_jobs(request: Request):
    user = get_current_user(request)
    user_id = user["id"] if user else None
    jobs = get_all_jobs(user_id=user_id)
    # Remove sensitive fields
    return [
        {k: v for k, v in job.items() if k not in ("video_s3_key", "original_file_s3_key", "api_key_hash")}
        for job in jobs
    ]


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    # Don't leak internal paths or API key hash
    public = {k: v for k, v in job.items() if k not in ("video_s3_key", "original_file_s3_key", "api_key_hash")}
    return public


@app.get("/api/jobs/{job_id}/video")
async def get_job_video(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
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

    # Check ownership if user is logged in
    user = get_current_user(request)
    if user and job.get("user_id") and job["user_id"] != user["id"]:
        raise HTTPException(403, "Not authorized to delete this job")

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
async def get_job_transcript(job_id: str):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    transcript_path = os.path.join(DATA_DIR, job_id, "transcript.json")
    if not os.path.exists(transcript_path):
        raise HTTPException(409, "Transcript not ready yet")

    with open(transcript_path, "r", encoding="utf-8") as f:
        return json.load(f)


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
async def regenerate_job_slides(job_id: str, request: RegenerateRequest):
    job = get_job_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if not job.get("video_ready"):
        raise HTTPException(409, "Cannot regenerate: original video not ready")

    job_dir = os.path.join(DATA_DIR, job_id)

    # Update status
    update_job(job_id, status="running", stage="regenerating", message="Regenerating slides...")

    # Start background thread for regeneration
    thread = threading.Thread(
        target=_regenerate_job,
        args=(job_id, job_dir, request.changes, request.api_key, job["voice"]),
        daemon=True,
    )
    thread.start()

    return {"status": "regenerating", "slides": [c.slide for c in request.changes]}


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

class TokenUpdate(BaseModel):
    name: Optional[str] = None
    token: Optional[str] = None
    provider: Optional[str] = None

@app.get("/api/tokens")
async def list_tokens(request: Request):
    """List all saved tokens (masked)."""
    user = get_current_user(request)
    user_id = user["id"] if user else None

    with get_db() as conn:
        cursor = conn.cursor()
        if user_id:
            cursor.execute("""
                SELECT id, name, provider, token_masked, created_at, last_used_at
                FROM tokens WHERE user_id = %s ORDER BY created_at DESC
            """, (user_id,))
        else:
            cursor.execute("""
                SELECT id, name, provider, token_masked, created_at, last_used_at
                FROM tokens WHERE user_id IS NULL ORDER BY created_at DESC
            """)
        rows = cursor.fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "provider": row["provider"],
                "token_masked": row["token_masked"],
                "created_at": row["created_at"].isoformat() if row["created_at"] else None,
                "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
            }
            for row in rows
        ]

@app.post("/api/tokens")
async def create_token(request: Request, data: TokenCreate):
    """Create a new token."""
    user = get_current_user(request)
    user_id = user["id"] if user else None

    token_id = str(uuid.uuid4())
    token_encrypted = encrypt_token(data.token)
    token_masked = mask_token(data.token)

    with DB_LOCK:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO tokens (id, user_id, name, provider, token_encrypted, token_masked)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING created_at
            """, (token_id, user_id, data.name, data.provider, token_encrypted, token_masked))
            result = cursor.fetchone()
            conn.commit()

    return {
        "id": token_id,
        "name": data.name,
        "provider": data.provider,
        "token_masked": token_masked,
        "created_at": result["created_at"].isoformat() if result else None,
    }

@app.get("/api/tokens/{token_id}")
async def get_token(token_id: str):
    """Get a token by ID (masked)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, name, provider, token_masked, created_at, last_used_at
            FROM tokens WHERE id = %s
        """, (token_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "Token not found")
        return {
            "id": row["id"],
            "name": row["name"],
            "provider": row["provider"],
            "token_masked": row["token_masked"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
        }

@app.get("/api/tokens/{token_id}/decrypt")
async def get_decrypted_token(token_id: str):
    """Get the decrypted token value (for internal use)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT token_encrypted FROM tokens WHERE id = %s", (token_id,))
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

        return {"token": decrypt_token(row["token_encrypted"])}

@app.put("/api/tokens/{token_id}")
async def update_token(token_id: str, data: TokenUpdate):
    """Update a token."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tokens WHERE id = %s", (token_id,))
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

        if updates:
            set_clause = ", ".join(f"{k} = %s" for k in updates.keys())
            values = list(updates.values()) + [token_id]
            with DB_LOCK:
                cursor.execute(f"UPDATE tokens SET {set_clause} WHERE id = %s", values)
                conn.commit()

    return {"status": "updated"}

@app.delete("/api/tokens/{token_id}")
async def delete_token(token_id: str):
    """Delete a token."""
    with DB_LOCK:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tokens WHERE id = %s", (token_id,))
            if cursor.rowcount == 0:
                raise HTTPException(404, "Token not found")
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


# Serve the simple frontend
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
