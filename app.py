from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from pathlib import Path
import os
import time
import shutil
from drive_client import DriveClient
import uuid
import subprocess
import tempfile
from typing import List, Optional
import cv2
from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect
import json

app = FastAPI()
drive_client = DriveClient()

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Directories
CLIPS_CACHE_DIR = Path("static/clips_cache")
RECORDINGS_DIR = Path("static/recordings")
CLIPS_CACHE_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(exist_ok=True)

# OpenCV Cascade Classifier for face detection
FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')

# Global state for managing ongoing recordings
recordings = {}

# Utility functions

def segment_video(video_path: str, clip_duration: int = 15) -> List[str]:
    """
    Segment video into clips of specified duration.
    Returns list of clip file paths.
    """
    try:
        duration_cmd = [
            'ffprobe', '-v', 'error', '-show_entries',
            'format=duration', '-of', 'csv=p=0', video_path
        ]
        result = subprocess.run(duration_cmd, capture_output=True, text=True, check=True)
        total_duration = float(result.stdout.strip())
        
        num_clips = int(total_duration // clip_duration) + (1 if total_duration % clip_duration > 5 else 0)
        
        video_name = Path(video_path).stem
        clips_dir = CLIPS_CACHE_DIR / video_name
        clips_dir.mkdir(exist_ok=True)
        
        clip_paths = []
        
        for i in range(num_clips):
            start_time = i * clip_duration
            clip_filename = f"{video_name}_clip_{i+1:03d}.mp4"
            clip_path = clips_dir / clip_filename
            
            cmd = [
                'ffmpeg', '-i', video_path,
                '-ss', str(start_time),
                '-t', str(clip_duration),
                '-c', 'copy',
                '-avoid_negative_ts', 'make_zero',
                str(clip_path),
                '-y'
            ]
            
            subprocess.run(cmd, capture_output=True, check=True)
            clip_paths.append(str(clip_path))
        
        return clip_paths
    
    except subprocess.CalledProcessError as e:
        raise Exception(f"FFmpeg error: {e.stderr.strip()}")
    except Exception as e:
        raise Exception(f"Video segmentation failed: {str(e)}")

def generate_video_stream(video_path: str, apply_opencv: bool = False):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise HTTPException(status_code=500, detail="Could not open video file.")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            if apply_opencv and FACE_CASCADE:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = FACE_CASCADE.detectMultiScale(gray, 1.1, 4)
                for (x, y, w, h) in faces:
                    cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 0, 0), 2)
            
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            
            time.sleep(0.01) # Control frame rate
    finally:
        cap.release()

def get_video_duration(video_path: str) -> float:
    try:
        duration_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', video_path]
        result = subprocess.run(duration_cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        print(f"Error getting duration: {e.stderr.strip()}")
        return 0.0

def stitch_clips(clip_paths: List[str], output_path: str):
    list_file_path = Path(tempfile.gettempdir()) / f"mylist_{uuid.uuid4()}.txt"
    with open(list_file_path, "w") as f:
        for path in clip_paths:
            f.write(f"file '{os.path.abspath(path)}'\n")
    
    cmd = [
        'ffmpeg', '-f', 'concat', '-safe', '0', '-i', str(list_file_path),
        '-c', 'copy', str(output_path), '-y'
    ]
    
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        print(f"FFmpeg stitching error: {e.stderr}")
        raise
    finally:
        list_file_path.unlink(missing_ok=True)


# Routes

@app.get("/", response_class=HTMLResponse)
async def viewer_dashboard(request: Request):
    data = drive_client.get_all_data()
    drive_space = drive_client.get_drive_space()
    return templates.TemplateResponse("viewer.html", {
        "request": request,
        "data": data,
        "drive_space": drive_space
    })

@app.get("/verifier", response_class=HTMLResponse)
async def verifier_dashboard(request: Request):
    data = drive_client.get_all_data()
    drive_space = drive_client.get_drive_space()
    return templates.TemplateResponse("verifier.html", {
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
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    age: str = Form(...),
    gender: str = Form(...),
    condition: str = Form(...),
    recording_id: str = Form(...),
    other_files: List[UploadFile] = File(None),
    clip_duration: int = Form(15)
):
    try:
        original_video_path = recordings.get(recording_id)
        if not original_video_path or not Path(original_video_path).exists():
            raise HTTPException(status_code=400, detail="Recording not found.")

        folder_name, folder_path = drive_client.create_local_folder()

        # Rename and move the original video
        timestamp = str(int(time.time()))
        unique_id = str(uuid.uuid4())[:8]
        video_ext = os.path.splitext(original_video_path)[1]
        original_video_filename = f"original_video_{timestamp}_{unique_id}{video_ext}"
        final_video_path = Path(folder_path) / original_video_filename
        shutil.move(original_video_path, final_video_path)

        # Create patient.txt
        info_content = (
            f"Name: {name}\nAge: {age}\nGender: {gender}\nCondition: {condition}\n"
            f"Verification Status: Pending\nReason: \nTimestamp: \n"
            f"Clip Duration: {clip_duration}s"
        )
        info_filename = "patient.txt"
        info_path = Path(folder_path) / info_filename
        with open(info_path, "w") as f:
            f.write(info_content)

        # Handle other files
        patient_info_dir = Path(folder_path) / "patient_info"
        patient_info_dir.mkdir(exist_ok=True)
        uploaded_other_files = []
        if other_files:
            for file in other_files:
                file_path = patient_info_dir / file.filename
                with open(file_path, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                drive_client.upload_to_drive(f"{folder_name}/patient_info", str(file_path))
                uploaded_other_files.append(file.filename)
        
        # Segment and upload clips
        print(f"Segmenting video into {clip_duration}s clips...")
        clip_paths = segment_video(str(final_video_path), clip_duration)
        
        clips_folder_name = f"{folder_name}/Clips"
        uploaded_clips = []
        for i, clip_path in enumerate(clip_paths, 1):
            clip_filename = f"Clip{i:03d}.mp4"
            drive_client.upload_to_drive(clips_folder_name, clip_path, file_name=clip_filename)
            uploaded_clips.append(clip_filename)
        
        # Upload main files
        drive_client.upload_to_drive(folder_name, str(final_video_path))
        drive_client.upload_to_drive(folder_name, str(info_path))
        
        # Clean up the local recording
        del recordings[recording_id]

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
        if 'folder_path' in locals() and os.path.exists(folder_path):
            shutil.rmtree(folder_path, ignore_errors=True)
        if 'original_video_path' in locals() and os.path.exists(original_video_path):
            os.remove(original_video_path)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/verification")
async def submit_verification(
    folder_name: str = Form(...),
    status: str = Form(...),
    reason: str = Form(...),
    timestamp: Optional[str] = Form(None)
):
    try:
        drive_client.update_verification_status(folder_name, status, reason, timestamp)
        return JSONResponse({"status": "success", "message": "Verification status updated."})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/data")
async def get_all_data():
    return drive_client.get_all_data()

@app.get("/api/drive_space")
async def get_drive_space():
    return drive_client.get_drive_space()

@app.get("/api/videos/{folder_name}/details")
async def get_video_details(folder_name: str):
    try:
        details = drive_client.get_folder_details(folder_name)
        if not details:
            raise HTTPException(status_code=404, detail="Video details not found.")
        return JSONResponse(details)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/videos/{folder_name}/play", response_class=StreamingResponse)
async def play_video_stream(folder_name: str, apply_opencv: bool = False):
    try:
        clips_dir = drive_client.download_clips_for_folder(folder_name)
        if not clips_dir:
            raise HTTPException(status_code=404, detail="Clips not found.")
            
        clip_paths = sorted(clips_dir.glob("*.mp4"))
        if not clip_paths:
            raise HTTPException(status_code=404, detail="No clips found in folder.")

        stitched_path = Path(tempfile.gettempdir()) / f"stitched_{uuid.uuid4()}.mp4"
        stitch_clips(clip_paths, str(stitched_path))
        
        return StreamingResponse(generate_video_stream(str(stitched_path), apply_opencv), media_type="multipart/x-mixed-replace; boundary=frame")
    
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws/record")
async def record_websocket(websocket: WebSocket):
    await websocket.accept()
    
    recording_id = str(uuid.uuid4())
    temp_file_path = RECORDINGS_DIR / f"{recording_id}.webm"
    recordings[recording_id] = str(temp_file_path)
    
    try:
        # Send a message to the client indicating the recording has started and provide the ID
        await websocket.send_json({"status": "recording_started", "id": recording_id})
        print(f"WebSocket connection opened. Recording ID: {recording_id}")

        # Use a context manager for the file to ensure it's closed correctly
        with open(temp_file_path, 'wb') as f:
            while True:
                # Receive data from the client
                data = await websocket.receive_bytes()
                f.write(data)

    except WebSocketDisconnect:
        # This exception is raised when the client closes the connection.
        print(f"Client disconnected from recording {recording_id}")

    except Exception as e:
        print(f"An error occurred during recording: {e}")

    finally:
        # This block runs after the `try` or `except` block completes.
        print(f"Finished recording session {recording_id}. File saved at {temp_file_path}")