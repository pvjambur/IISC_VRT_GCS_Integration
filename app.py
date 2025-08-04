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

app = FastAPI()
drive_client = DriveClient()

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

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
    video: UploadFile = File(...)
):
    try:
        # Create local folder
        folder_name, folder_path = drive_client.create_local_folder()
        
        # Generate unique filenames
        timestamp = str(int(time.time()))
        unique_id = str(uuid.uuid4())[:8]
        
        # Save video file
        video_ext = os.path.splitext(video.filename)[1]
        video_filename = f"video_{timestamp}_{unique_id}{video_ext}"
        video_path = os.path.join(folder_path, video_filename)
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(video.file, buffer)
        
        # Create info file
        info_content = f"Name: {name}\nAge: {age}\nGender: {gender}\nCondition: {condition}"
        info_filename = f"info_{timestamp}_{unique_id}.txt"
        info_path = os.path.join(folder_path, info_filename)
        with open(info_path, "w") as f:
            f.write(info_content)
        
        # Upload to Google Drive
        drive_client.upload_to_drive(folder_name, video_path)
        drive_client.upload_to_drive(folder_name, info_path)
        
        # Update local folders cache
        drive_client.local_folders = drive_client._scan_local_folders()
        
        return JSONResponse({
            "status": "success",
            "folder": folder_name,
            "video": video_filename,
            "info": info_filename
        })
    except Exception as e:
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)