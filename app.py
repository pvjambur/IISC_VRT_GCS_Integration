from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from pathlib import Path
import os
import time
from datetime import datetime
from drive_client import DriveClient
import shutil

app = FastAPI()
drive_client = DriveClient()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/upload")
async def upload_video(
    request: Request,
    name: str = Form(...),
    age: str = Form(...),
    gender: str = Form(...),
    condition: str = Form(...),
    video: UploadFile = File(...)
):
    try:
        # Create a temporary directory for upload
        timestamp = str(int(time.time()))
        temp_dir = Path("static/temp") / timestamp
        temp_dir.mkdir(parents=True, exist_ok=True)
        
        # Save the video file temporarily
        video_filename = f"video_{timestamp}{Path(video.filename).suffix}"
        video_path = temp_dir / video_filename
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)
        
        # Create information file
        info_content = f"Name: {name}\nAge: {age}\nGender: {gender}\nCondition: {condition}"
        info_filename = f"information_{timestamp}.txt"
        info_path = temp_dir / info_filename
        with open(info_path, "w") as f:
            f.write(info_content)
        
        # Create folder in Google Drive
        folder_id = drive_client.create_data_folder()
        
        # Upload files sequentially
        video_id = drive_client.upload_file_to_folder(folder_id, str(video_path))
        info_id = drive_client.upload_file_to_folder(folder_id, str(info_path))
        
        # Clean up temp files
        shutil.rmtree(temp_dir)
        
        return JSONResponse({
            "status": "success",
            "message": "Files uploaded successfully",
            "folder_id": folder_id,
            "video_id": video_id,
            "info_id": info_id
        })
    except Exception as e:
        # Clean up temp files if they exist
        if 'temp_dir' in locals() and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/upload_progress/{filename}")
async def get_upload_progress(filename: str):
    progress = drive_client.get_upload_progress(filename)
    return {"progress": progress}

@app.get("/drive_space")
async def get_drive_space():
    return drive_client.drive_space

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)