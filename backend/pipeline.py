import asyncio
import base64
import json
import os
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


NARRATION_API_URL = "https://hnd1.aihub.zeabur.ai/v1/chat/completions"


def get_slide_narration(
    image_path: str,
    api_key: str,
    model: str = "claude-sonnet-4-5",
    max_retries: int = 3,
    timeout: int = 60,
) -> str:
    """Uses the Zeabur AI Hub OpenAI-compatible chat completions endpoint
    (same one the original notebook used)."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": NARRATION_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    last_err = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                NARRATION_API_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"].strip()
        except requests.exceptions.RequestException as e:
            last_err = e
            if attempt < max_retries:
                import time
                time.sleep(3)
    raise RuntimeError(f"Narration request failed after {max_retries} attempts: {last_err}")


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
    progress_cb: Optional[ProgressCB] = None,
) -> str:
    """Runs the full pipeline for one uploaded file. Returns path to final mp4."""

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
        narration = get_slide_narration(image_path, api_key, model=model)

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

    report("done", total, total, "Done")
    return final_path