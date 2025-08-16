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
import logging
import threading
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import asyncio

# Configure logging to show info messages
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
drive_client = DriveClient()

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Directories
CLIPS_CACHE_DIR = Path("static/clips_cache")
TEMP_DIR = Path("static/temp")
CLIPS_CACHE_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)

# OpenCV Cascade Classifier for face detection
try:
    FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
except Exception as e:
    logger.error(f"Error loading OpenCV cascade classifier: {e}. Face detection will be disabled.")
    FACE_CASCADE = None

# Global state for managing ongoing recordings
active_recordings = {}

class TempFolderHandler(FileSystemEventHandler):
    """Handler for monitoring changes in temp folder"""
    
    def __init__(self, drive_client):
        self.drive_client = drive_client
        super().__init__()
    
    def on_created(self, event):
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        # Check if the created file is an MP4 inside a 'Clips' subfolder
        if file_path.suffix == '.mp4' and file_path.parent.name == 'Clips':
            folder_name = file_path.parent.parent.name  # DataX folder name
            self.auto_upload_clip(folder_name, str(file_path))
    
    def auto_upload_clip(self, folder_name: str, clip_path: str):
        """Automatically upload clip to Google Drive"""
        try:
            clips_drive_path = f"{folder_name}/Clips"
            clip_filename = Path(clip_path).name
            
            logger.info(f"Auto-uploading clip: {clip_filename} to {clips_drive_path}")
            file_id = self.drive_client.upload_to_drive(clips_drive_path, clip_path, file_name=clip_filename)
            logger.info(f"Successfully uploaded {clip_filename} with ID: {file_id}")
            
        except Exception as e:
            logger.error(f"Failed to auto-upload clip {clip_path}: {e}")
            # Don't raise the exception to prevent the file watcher from stopping

# Initialize file system watcher
temp_handler = TempFolderHandler(drive_client)
observer = Observer()
observer.schedule(temp_handler, str(TEMP_DIR), recursive=True)
observer.start()

# Utility functions

def get_next_data_folder() -> str:
    """Get the next available DataX folder name"""
    # Use drive client to get consistent numbering with Google Drive
    return drive_client.get_next_folder_name_from_drive()

def create_recording_folder() -> tuple[str, Path, Path]:
    """Create a new DataX folder with a Clips subfolder"""
    folder_name = get_next_data_folder()
    folder_path = TEMP_DIR / folder_name
    clips_path = folder_path / "Clips"
    
    folder_path.mkdir(exist_ok=True)
    clips_path.mkdir(exist_ok=True)
    
    logger.info(f"Created recording folder: {folder_path} with Clips subfolder: {clips_path}")
    return folder_name, folder_path, clips_path

def segment_video(video_path: str, clip_duration: int = 15) -> List[str]:
    """
    Segment video into clips of specified duration.
    Returns list of clip file paths.
    """
    logger.info(f"Starting video segmentation for {video_path}")
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
        
        logger.info(f"Video segmentation complete. {len(clip_paths)} clips created.")
        return clip_paths
    
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg error: {e.stderr.strip()}")
        raise Exception(f"FFmpeg error: {e.stderr.strip()}")
    except Exception as e:
        logger.error(f"Video segmentation failed: {str(e)}")
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
        logger.error(f"Error getting duration: {e.stderr.strip()}")
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
        logger.info(f"Clips stitched successfully to {output_path}")
    except subprocess.CalledProcessError as e:
        logger.error(f"FFmpeg stitching error: {e.stderr}")
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
    folder_name: str = Form(...),
    clip_duration: int = Form(15),
    other_files: List[UploadFile] = File(None)
):
    try:
        # Check if the folder exists in temp
        folder_path = TEMP_DIR / folder_name
        if not folder_path.exists():
            raise HTTPException(status_code=400, detail="Recording folder not found.")

        logger.info(f"Processing upload for folder: {folder_path}")

        # Create patient.txt
        info_content = (
            f"Name: {name}\nAge: {age}\nGender: {gender}\nCondition: {condition}\n"
            f"Verification Status: Pending\nReason: \nTimestamp: \n"
            f"Clip Duration: {clip_duration}s"
        )
        info_filename = "patient.txt"
        info_path = folder_path / info_filename
        with open(info_path, "w") as f:
            f.write(info_content)
        logger.info(f"Created patient info file: {info_path}")

        # Handle other files
        patient_info_dir = folder_path / "patient_info"
        patient_info_dir.mkdir(exist_ok=True)
        uploaded_other_files = []
        if other_files:
            for file in other_files:
                if file.filename:  # Check if file has a name
                    file_path = patient_info_dir / file.filename
                    with open(file_path, "wb") as buffer:
                        shutil.copyfileobj(file.file, buffer)
                    drive_client.upload_to_drive(f"{folder_name}/patient_info", str(file_path))
                    uploaded_other_files.append(file.filename)
            logger.info(f"Uploaded {len(uploaded_other_files)} additional files.")
        
        # Upload patient.txt to drive
        drive_client.upload_to_drive(folder_name, str(info_path))
        
        # Get list of clips that were already uploaded automatically
        clips = drive_client.get_clips_in_folder(folder_name)
        
        return JSONResponse({
            "status": "success",
            "folder": folder_name,
            "clips_folder": f"{folder_name}/Clips",
            "info": info_filename,
            "total_clips": len(clips),
            "clips": clips,
            "clip_duration": clip_duration,
            "other_files": uploaded_other_files
        })
    
    except Exception as e:
        logger.error(f"Upload failed: {e}")
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
    
    try:
        # Create recording folder structure
        folder_name, folder_path, clips_path = create_recording_folder()
        
        # Send the folder name to the client
        await websocket.send_json({
            "status": "recording_started",
            "folder_name": folder_name,
            "clips_path": str(clips_path)
        })
        
        logger.info(f"WebSocket connection opened. Folder: {folder_name}")
        
        clip_number = 1
        
        while True:
            try:
                # Receive the full video clip as binary data
                data = await websocket.receive_bytes()
                
                if data:
                    clip_filename = f"Clip{clip_number:03d}.webm"
                    clip_path = clips_path / clip_filename
                    
                    with open(clip_path, 'wb') as f:
                        f.write(data)
                        
                    logger.info(f"Received and saved clip: {clip_path}")
                    
                    # Notify client about new clip
                    await websocket.send_json({
                        "status": "new_clip_saved",
                        "clip_number": clip_number
                    })
                    
                    clip_number += 1
            
            except WebSocketDisconnect:
                break
            except Exception as e:
                logger.error(f"Error during recording: {e}")
                break

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from recording")
    except Exception as e:
        logger.error(f"An error occurred during recording: {e}")
    finally:
        # Clean up active recording (if any)
        if 'folder_name' in locals() and folder_name in active_recordings:
            del active_recordings[folder_name]
        logger.info(f"Recording session ended")

@app.on_event("shutdown")
def shutdown_event():
    observer.stop()
    observer.join()