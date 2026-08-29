import asyncio
import base64
import json
import os
import re
import subprocess
from typing import Callable, List, Optional

import requests
from pdf2image import convert_from_path
import edge_tts

# ---------------------------------------------------------------------------
# Step 1: normalize input (pptx or pdf) into per-slide PNG images
# ---------------------------------------------------------------------------

def extract_slide_images(input_path: str, output_dir: str) -> List[str]:
    os.makedirs(output_dir, exist_ok=True)

    if input_path.lower().endswith(".pptx"):
        # LibreOffice headless conversion: pptx -> pdf
        subprocess.run(
            [
                "soffice", "--headless", "--convert-to", "pdf",
                "--outdir", output_dir, input_path,
            ],
            check=True,
            timeout=300,
        )
        pdf_path = os.path.join(
            output_dir, os.path.splitext(os.path.basename(input_path))[0] + ".pdf"
        )
    elif input_path.lower().endswith(".pdf"):
        pdf_path = input_path
    else:
        raise ValueError("Input must be .pptx or .pdf")

    pages = convert_from_path(pdf_path, dpi=200)
    image_paths = []
    for i, page in enumerate(pages, start=1):
        img_path = os.path.join(output_dir, f"slide_{i:02d}.png")
        page.save(img_path, "PNG")
        image_paths.append(img_path)

    return image_paths


# ---------------------------------------------------------------------------
# Step 2: ask an LLM to narrate the slide, given the slide image
# ---------------------------------------------------------------------------

NARRATION_PROMPT = (
    "Describe this presentation slide as natural spoken narration a presenter "
    "would say aloud. Do not start with a preamble like 'Here's how a "
    "presenter might narrate this slide:' -- just give the narration itself."
)


# ---------------------------------------------------------------------------
# Multi-provider LLM dispatch. Every saved token has a provider ("anthropic",
# "openai", or "other" with a user-supplied host from Manage Tokens), and
# every AI call in this file goes through chat_completion() below instead of
# one hardcoded endpoint, so whichever provider the caller's token is for
# gets used correctly.
# ---------------------------------------------------------------------------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


def _openai_style_completion(prompt: str, api_key: str, model: str, url: str, timeout: int, image_b64: Optional[str] = None) -> str:
    """OpenAI-compatible chat completions -- used for OpenAI itself, and for
    'other' (any self-hosted or third-party OpenAI-compatible proxy)."""
    content = [{"type": "text", "text": prompt}]
    if image_b64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}})
    payload = {"model": model, "messages": [{"role": "user", "content": content}]}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip()


def _anthropic_completion(prompt: str, api_key: str, model: str, timeout: int, image_b64: Optional[str] = None) -> str:
    content = []
    if image_b64:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}})
    content.append({"type": "text", "text": prompt})
    payload = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": content}]}
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()["content"][0]["text"].strip()


def chat_completion(
    prompt: str,
    api_key: str,
    model: str,
    provider: str = "anthropic",
    provider_host: Optional[str] = None,
    timeout: int = 30,
    image_b64: Optional[str] = None,
) -> str:
    """Dispatch one prompt (optionally with an image) to the token's
    provider, return the raw text reply."""
    provider = (provider or "anthropic").lower()
    if provider == "anthropic":
        return _anthropic_completion(prompt, api_key, model, timeout, image_b64=image_b64)
    if provider == "openai":
        return _openai_style_completion(prompt, api_key, model, OPENAI_API_URL, timeout, image_b64=image_b64)
    if provider == "other":
        if not provider_host:
            raise ValueError("This token's provider is 'Other' but has no API host set. Edit it in Manage Tokens.")
        return _openai_style_completion(prompt, api_key, model, provider_host, timeout, image_b64=image_b64)
    raise ValueError(f"Unknown provider: {provider}")


def get_slide_narration(
    image_path: str,
    api_key: str,
    model: str = "claude-sonnet-4-5",
    provider: str = "anthropic",
    provider_host: Optional[str] = None,
    max_retries: int = 3,
    timeout: int = 60,
) -> str:
    """Ask the LLM to narrate one slide, given its rendered image."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            return chat_completion(NARRATION_PROMPT, api_key, model, provider, provider_host, timeout=timeout, image_b64=image_b64)
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < max_retries:
                import time
                time.sleep(3)
    raise RuntimeError(f"Narration request failed after {max_retries} attempts: {last_err}")


def _parse_json_reply(content: str) -> dict:
    """Models sometimes wrap JSON in a markdown fence or add prose around it."""
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    match = re.search(r"\{.*\}", content, re.S)
    return json.loads(match.group(0) if match else content)


def _script_from_transcript(transcript: List[dict]) -> str:
    return "\n".join(f"Slide {s['slide']}: {s['text']}" for s in transcript)


GENERATE_TITLE_DESCRIPTION_PROMPT = (
    "Here is the slide-by-slide narration script for a presentation. Based on "
    "it, suggest a short, descriptive title (under 8 words, no quotes) and a "
    "one-sentence description (under 25 words) that together would help "
    "someone recognize this presentation in a list of many.\n\n"
    "{script}\n\n"
    "Respond with ONLY a JSON object, no other text, in this exact shape: "
    '{{"title": "...", "description": "..."}}'
)

IMPROVE_TITLE_DESCRIPTION_PROMPT = (
    "Here is a presentation's current title and description, and its full "
    "narration script for context. Improve the wording, grammar, and clarity "
    "of the title and description WITHOUT changing the information they "
    "convey. If either is blank, write one from scratch based on the script.\n\n"
    "Current title: {title}\n"
    "Current description: {description}\n\n"
    "Narration script:\n{script}\n\n"
    "Respond with ONLY a JSON object, no other text, in this exact shape: "
    '{{"title": "...", "description": "..."}}'
)


def _clean_title_description(parsed: dict) -> dict:
    return {
        "title": (parsed.get("title") or "").strip()[:200] or None,
        "description": (parsed.get("description") or "").strip()[:1000] or None,
    }


def generate_title_description(
    transcript: List[dict],
    api_key: str,
    model: str = "claude-sonnet-4-5",
    provider: str = "anthropic",
    provider_host: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Suggest a fresh title + description for the whole deck, from scratch."""
    prompt = GENERATE_TITLE_DESCRIPTION_PROMPT.format(script=_script_from_transcript(transcript))
    content = chat_completion(prompt, api_key, model, provider, provider_host, timeout=timeout)
    return _clean_title_description(_parse_json_reply(content))


def improve_title_description(
    current_title: Optional[str],
    current_description: Optional[str],
    transcript: List[dict],
    api_key: str,
    model: str = "claude-sonnet-4-5",
    provider: str = "anthropic",
    provider_host: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """Polish an existing title + description without changing their meaning."""
    prompt = IMPROVE_TITLE_DESCRIPTION_PROMPT.format(
        title=current_title or "(none)",
        description=current_description or "(none)",
        script=_script_from_transcript(transcript),
    )
    content = chat_completion(prompt, api_key, model, provider, provider_host, timeout=timeout)
    return _clean_title_description(_parse_json_reply(content))


IMPROVE_NARRATION_PROMPT = (
    "Here is spoken narration for one slide of a presentation. Improve its "
    "grammar, clarity, and flow as a presenter would say it aloud, without "
    "changing its meaning or the information it conveys. Do not add a "
    "preamble or explanation -- respond with ONLY the improved narration "
    "text.\n\n{text}"
)

CUSTOM_NARRATION_PROMPT = (
    "Here is spoken narration for one slide of a presentation:\n\n{text}\n\n"
    "Rewrite it according to this instruction: {instruction}\n"
    "Keep it as natural spoken narration a presenter would say aloud. Do not "
    "add a preamble or explanation -- respond with ONLY the rewritten "
    "narration text."
)


def edit_slide_narration(
    current_text: str,
    api_key: str,
    model: str = "claude-sonnet-4-5",
    instruction: Optional[str] = None,
    provider: str = "anthropic",
    provider_host: Optional[str] = None,
    timeout: int = 30,
) -> str:
    """Improve existing narration, or rewrite it per a custom instruction."""
    prompt = (
        CUSTOM_NARRATION_PROMPT.format(text=current_text, instruction=instruction)
        if instruction
        else IMPROVE_NARRATION_PROMPT.format(text=current_text)
    )
    return chat_completion(prompt, api_key, model, provider, provider_host, timeout=timeout)


# ---------------------------------------------------------------------------
# Step 3: text -> speech, and video assembly (ffmpeg)
# ---------------------------------------------------------------------------

async def narration_to_speech(text: str, output_path: str, voice: str = "en-US-ChristopherNeural"):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(output_path)


def get_audio_duration(audio_path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def make_slide_video(image_path: str, audio_path: str, output_path: str):
    duration = get_audio_duration(audio_path)
    cmd = [
        "ffmpeg", "-loop", "1", "-i", image_path, "-i", audio_path,
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-t", str(duration),
        "-fps_mode", "cfr",
        "-y", output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def concatenate_videos(video_paths: List[str], output_path: str, work_dir: str):
    list_file = os.path.join(work_dir, "video_list.txt")
    with open(list_file, "w") as f:
        for v in video_paths:
            f.write(f"file '{os.path.abspath(v)}'\n")

    cmd = [
        "ffmpeg", "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-fflags", "+genpts",
        "-avoid_negative_ts", "make_zero",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-y", output_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Orchestration: this is what the web job runner calls
# ---------------------------------------------------------------------------

ProgressCB = Callable[[str, Optional[int], Optional[int], Optional[str]], None]
# progress_cb(stage, current_slide, total_slides, message)


def regenerate_slides(
    job_dir: str,
    changes: dict,  # {slide_number: new_text}
    api_key: str,
    voice: str = "en-US-ChristopherNeural",
    progress_cb: Optional[ProgressCB] = None,
) -> str:
    """Regenerate only the changed slides and merge with existing ones."""

    def report(stage, cur=None, total=None, msg=None):
        if progress_cb:
            progress_cb(stage, cur, total, msg)

    slides_dir = os.path.join(job_dir, "slides")
    transcript_path = os.path.join(job_dir, "transcript.json")

    # Load existing transcript
    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = json.load(f)

    total_slides = len(transcript)
    changed_slides = list(changes.keys())
    total_changes = len(changed_slides)

    report("regenerating", 0, total_changes, f"Regenerating {total_changes} slide(s)...")

    # Regenerate changed slides
    for idx, slide_num in enumerate(changed_slides, start=1):
        new_text = changes[slide_num]

        report("speech", idx, total_changes, f"Generating audio for slide {slide_num}")

        # Update narration text file
        narration_txt_path = os.path.join(slides_dir, f"slide_{slide_num:02d}_narration.txt")
        with open(narration_txt_path, "w", encoding="utf-8") as f:
            f.write(new_text)

        # Generate new audio
        audio_path = os.path.join(slides_dir, f"slide_{slide_num:02d}_narration.mp3")
        asyncio.run(narration_to_speech(new_text, audio_path, voice=voice))

        report("rendering", idx, total_changes, f"Rendering video for slide {slide_num}")

        # Generate new video for this slide
        image_path = os.path.join(slides_dir, f"slide_{slide_num:02d}.png")
        video_path = os.path.join(slides_dir, f"slide_{slide_num:02d}.mp4")
        make_slide_video(image_path, audio_path, video_path)

    # Rebuild transcript with new durations
    report("finalizing", total_changes, total_changes, "Updating transcript...")

    cumulative_time = 0.0
    new_transcript = []
    video_paths = []

    for i in range(1, total_slides + 1):
        audio_path = os.path.join(slides_dir, f"slide_{i:02d}_narration.mp3")
        video_path = os.path.join(slides_dir, f"slide_{i:02d}.mp4")
        narration_txt_path = os.path.join(slides_dir, f"slide_{i:02d}_narration.txt")

        duration = get_audio_duration(audio_path)

        with open(narration_txt_path, "r", encoding="utf-8") as f:
            text = f.read()

        new_transcript.append({
            "slide": i,
            "text": text,
            "start": round(cumulative_time, 2),
            "duration": round(duration, 2),
        })
        cumulative_time += duration
        video_paths.append(video_path)

    # Concatenate all videos
    report("concatenating", total_changes, total_changes, "Combining slides into final video...")
    final_path = os.path.join(job_dir, "final_presentation.mp4")
    concatenate_videos(video_paths, final_path, slides_dir)

    # Save updated transcript
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(new_transcript, f, ensure_ascii=False, indent=2)

    report("done", total_changes, total_changes, "Done")
    return final_path


def run_pipeline(
    input_path: str,
    job_dir: str,
    api_key: str,
    voice: str = "en-US-ChristopherNeural",
    model: str = "claude-sonnet-4-5",
    provider: str = "anthropic",
    provider_host: Optional[str] = None,
    progress_cb: Optional[ProgressCB] = None,
) -> dict:
    """Runs the full pipeline for one uploaded file. Returns
    {"video_path": ..., "title": ..., "description": ...} -- title/description
    are a best-effort AI suggestion and may be None if that step fails."""

    def report(stage, cur=None, total=None, msg=None):
        if progress_cb:
            progress_cb(stage, cur, total, msg)

    slides_dir = os.path.join(job_dir, "slides")
    os.makedirs(slides_dir, exist_ok=True)

    report("extracting", msg="Converting file to slide images")
    slide_images = extract_slide_images(input_path, slides_dir)
    total = len(slide_images)
    if total == 0:
        raise RuntimeError("No slides were extracted from the input file.")

    per_slide_videos = []
    narrations = []
    cumulative_time = 0.0
    for i, image_path in enumerate(slide_images, start=1):
        report("narrating", i, total, f"Narrating slide {i}/{total}")
        narration = get_slide_narration(image_path, api_key, model=model, provider=provider, provider_host=provider_host)

        narration_txt_path = os.path.join(slides_dir, f"slide_{i:02d}_narration.txt")
        with open(narration_txt_path, "w", encoding="utf-8") as f:
            f.write(narration)

        report("speech", i, total, f"Generating audio for slide {i}/{total}")
        audio_path = os.path.join(slides_dir, f"slide_{i:02d}_narration.mp3")
        asyncio.run(narration_to_speech(narration, audio_path, voice=voice))
        duration = get_audio_duration(audio_path)

        report("rendering", i, total, f"Rendering video for slide {i}/{total}")
        video_path = os.path.join(slides_dir, f"slide_{i:02d}.mp4")
        make_slide_video(image_path, audio_path, video_path)
        per_slide_videos.append(video_path)

        narrations.append({
            "slide": i,
            "text": narration,
            "start": round(cumulative_time, 2),
            "duration": round(duration, 2),
        })
        cumulative_time += duration

    report("concatenating", total, total, "Combining all slides into final video")
    final_path = os.path.join(job_dir, "final_presentation.mp4")
    concatenate_videos(per_slide_videos, final_path, slides_dir)

    # Written alongside the video so the frontend can fetch the full,
    # timestamped narration transcript for the "read along" panel.
    transcript_path = os.path.join(job_dir, "transcript.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(narrations, f, ensure_ascii=False, indent=2)

    report("naming", total, total, "Naming the presentation")
    title, description = None, None
    try:
        suggestion = generate_title_description(narrations, api_key, model=model, provider=provider, provider_host=provider_host)
        title, description = suggestion["title"], suggestion["description"]
    except Exception:
        pass  # naming is a nice-to-have; never fail the whole job over it

    report("done", total, total, "Done")
    return {"video_path": final_path, "title": title, "description": description}