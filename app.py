from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pathlib import Path
import os
import time
import shutil
from drive_client import DriveClient
import uuid
import subprocess
import tempfile
from typing import List

app = FastAPI()
drive_client = DriveClient()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Create clips cache directory
CLIPS_CACHE_DIR = Path("static/clips_cache")
CLIPS_CACHE_DIR.mkdir(exist_ok=True)

def segment_video(video_path: str, clip_duration: int = 15) -> List[str]:
    """
    Segment video into clips of specified duration
    Returns list of clip file paths
    """
    try:
        # Get video duration first
        duration_cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 
            'format=duration', '-of', 'csv=p=0', video_path
        ]
        result = subprocess.run(duration_cmd, capture_output=True, text=True)
        total_duration = float(result.stdout.strip())
        
        # Calculate number of clips
        num_clips = int(total_duration // clip_duration) + (1 if total_duration % clip_duration > 5 else 0)
        
        # Create clips directory in cache
        video_name = Path(video_path).stem
        clips_dir = CLIPS_CACHE_DIR / video_name
        clips_dir.mkdir(exist_ok=True)
        
        clip_paths = []
        
        for i in range(num_clips):
            start_time = i * clip_duration
            clip_filename = f"{video_name}_clip_{i+1:03d}.mp4"
            clip_path = clips_dir / clip_filename
            
            # FFmpeg command to create clip
            cmd = [
                'ffmpeg', '-i', video_path,
                '-ss', str(start_time),
                '-t', str(clip_duration),
                '-c', 'copy',  # Copy without re-encoding for speed
                '-avoid_negative_ts', 'make_zero',
                str(clip_path),
                '-y'  # Overwrite if exists
            ]
            
            subprocess.run(cmd, capture_output=True, check=True)
            clip_paths.append(str(clip_path))
        
        return clip_paths
    
    except subprocess.CalledProcessError as e:
        raise Exception(f"FFmpeg error: {e}")
    except Exception as e:
        raise Exception(f"Video segmentation failed: {str(e)}")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    data = drive_client.get_all_data()
    drive_space = drive_client.get_drive_space()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "data": data,
        "drive_space": drive_space
    })

@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    drive_space = drive_client.get_drive_space()
    return templates.TemplateResponse("upload.html", {
        "request": request,
        "drive_space": drive_space
    })

@app.post("/api/upload")
async def upload_data(
    name: str = Form(...),
    age: str = Form(...),
    gender: str = Form(...),
    condition: str = Form(...),
    video: UploadFile = File(...),
    clip_duration: int = Form(15)  # Default 15 seconds per clip
):
    try:
        # Create local folder
        folder_name, folder_path = drive_client.create_local_folder()
        
        # Generate unique filenames
        timestamp = str(int(time.time()))
        unique_id = str(uuid.uuid4())[:8]
        
        # Save original video file temporarily
        video_ext = os.path.splitext(video.filename)[1]
        original_video_filename = f"original_video_{timestamp}_{unique_id}{video_ext}"
        original_video_path = os.path.join(folder_path, original_video_filename)
        
        with open(original_video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)
        
        # Create info file
        info_content = f"Name: {name}\nAge: {age}\nGender: {gender}\nCondition: {condition}\nClip Duration: {clip_duration}s"
        info_filename = f"info_{timestamp}_{unique_id}.txt"
        info_path = os.path.join(folder_path, info_filename)
        with open(info_path, "w") as f:
            f.write(info_content)
        
        # Segment the video into clips
        print(f"Segmenting video into {clip_duration}s clips...")
        clip_paths = segment_video(original_video_path, clip_duration)
        
        # Upload original video and info to Drive
        drive_client.upload_to_drive(folder_name, original_video_path)
        drive_client.upload_to_drive(folder_name, info_path)
        
        # Create clips folder in Drive and upload clips
        clips_folder_name = f"{folder_name}_clips"
        uploaded_clips = []
        
        for i, clip_path in enumerate(clip_paths, 1):
            clip_filename = f"clip_{i:03d}_{timestamp}_{unique_id}.mp4"
            
            # Copy clip to main folder with proper naming
            main_folder_clip_path = os.path.join(folder_path, clip_filename)
            shutil.copy2(clip_path, main_folder_clip_path)
            
            # Upload clip to Drive
            drive_client.upload_to_drive(clips_folder_name, main_folder_clip_path)
            uploaded_clips.append(clip_filename)
            
            print(f"Uploaded clip {i}/{len(clip_paths)}: {clip_filename}")
        
        # Clean up original video file (keep clips in cache for serving)
        # os.remove(original_video_path)  # Uncomment if you don't want to keep original
        
        # Update local folders cache
        drive_client.local_folders = drive_client._scan_local_folders()
        
        return JSONResponse({
            "status": "success",
            "folder": folder_name,
            "clips_folder": clips_folder_name,
            "original_video": original_video_filename,
            "info": info_filename,
            "clips": uploaded_clips,
            "total_clips": len(clip_paths),
            "clip_duration": clip_duration
        })
        
    except Exception as e:
        # Clean up on error
        if 'folder_path' in locals() and os.path.exists(folder_path):
            shutil.rmtree(folder_path, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/data")
async def get_all_data():
    return drive_client.get_all_data()

@app.get("/api/drive_space")
async def get_drive_space():
    return drive_client.get_drive_space()

@app.get("/videos/{folder}/{filename}")
async def get_video(folder: str, filename: str):
    video_path = Path("static/temp") / folder / filename
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(video_path)

@app.get("/clips/{video_name}/{clip_filename}")
async def get_clip(video_name: str, clip_filename: str):
    """Serve video clips from cache"""
    clip_path = CLIPS_CACHE_DIR / video_name / clip_filename
    if not clip_path.exists():
        raise HTTPException(status_code=404, detail="Clip not found")
    return FileResponse(clip_path)

@app.get("/api/clips/{video_name}")
async def list_clips(video_name: str):
    """List all clips for a specific video"""
    clips_dir = CLIPS_CACHE_DIR / video_name
    if not clips_dir.exists():
        return JSONResponse({"clips": []})
    
    clips = []
    for clip_file in sorted(clips_dir.glob("*.mp4")):
        clips.append({
            "filename": clip_file.name,
            "path": f"/clips/{video_name}/{clip_file.name}",
            "size": clip_file.stat().st_size
        })
    
    return JSONResponse({"clips": clips})

@app.delete("/api/clips/cache")
async def clear_clips_cache():
    """Clear the clips cache to free up space"""
    try:
        if CLIPS_CACHE_DIR.exists():
            shutil.rmtree(CLIPS_CACHE_DIR)
            CLIPS_CACHE_DIR.mkdir(exist_ok=True)
        return JSONResponse({"status": "success", "message": "Clips cache cleared"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
