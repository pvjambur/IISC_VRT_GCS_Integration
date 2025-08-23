import os
import csv
import json
import uuid
import pandas as pd
from fastapi import FastAPI, UploadFile, File, HTTPException, status
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime
from drive_client import DriveClient
from pathlib import Path
import asyncio
import aiofiles
import logging
import shutil

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    drive_client = DriveClient()
except Exception as e:
    logger.error(f"Failed to initialize DriveClient: {e}. The app may not function as expected.")
    drive_client = None

USERS_DB = "user.csv"
DATA_DB = "data.csv"
STATIC_DIR = "static"
TEMP_DIR = Path(STATIC_DIR) / "temp"
CLIPS_CACHE_DIR = Path(STATIC_DIR) / "clips_cache"

Path(STATIC_DIR).mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)
CLIPS_CACHE_DIR.mkdir(exist_ok=True)

if not Path(USERS_DB).exists():
    with open(USERS_DB, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["email", "password", "role"])

def seed_data_csv():
    if Path(DATA_DB).stat().st_size == 0:
        sample_data = [
            {
                "Folder_Name": "UHID12345",
                "JSON_Details": json.dumps({
                    "Name": "Aarav Sharma", "Age": "2 months", "Gender": "Male", "DOB": "2025-05-12",
                    "CDoB": "2025-05-20", "GA": "38 weeks", "ChronoAge": "2 months 1 week",
                    "Location": "Mumbai, India", "DeviceInfo": "Samsung Galaxy Tab S8", "Comments": "",
                    "GMAE_status": "Pending", "VideoQ_status": "Good"
                }),
                "VideoQ_status": "Good", "GMAE_status": "Pending"
            },
            {
                "Folder_Name": "UHID55678",
                "JSON_Details": json.dumps({
                    "Name": "Aarav Gupta", "Age": "2 months", "Gender": "Male", "DOB": "2025-05-12",
                    "CDoB": "2025-05-20", "GA": "38 weeks", "ChronoAge": "2 months 1 week",
                    "Location": "Mumbai, India", "DeviceInfo": "Samsung Galaxy Tab S8", "Comments": "",
                    "GMAE_status": "Approved", "VideoQ_status": "Good"
                }),
                "VideoQ_status": "Good", "GMAE_status": "Approved"
            },
            {
                "Folder_Name": "UHID44567",
                "JSON_Details": json.dumps({
                    "Name": "Kavya Iyer", "Age": "3 months", "Gender": "Female", "DOB": "2025-04-01",
                    "CDoB": "2025-04-08", "GA": "38 weeks", "ChronoAge": "3 months 1 week",
                    "Location": "Bangalore, India", "DeviceInfo": "iPad Air", "Comments": "Poor video quality, retake required.",
                    "GMAE_status": "Rejected", "VideoQ_status": "Poor"
                }),
                "VideoQ_status": "Poor", "GMAE_status": "Rejected"
            }
        ]
        df = pd.DataFrame(sample_data)
        df.to_csv(DATA_DB, index=False)
        print("Seeded data.csv with initial data.")

if not Path(DATA_DB).exists():
    with open(DATA_DB, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Folder_Name", "JSON_Details", "VideoQ_status", "GMAE_status"])
    seed_data_csv()
else:
    # Check if the file is empty and seed if so
    if Path(DATA_DB).stat().st_size == 0:
        seed_data_csv()


# Pydantic models
class User(BaseModel):
    email: str
    password: str

class UserSignup(BaseModel):
    fullName: str
    email: str
    phone: str
    password: str

class VideoMetadata(BaseModel):
    name: str
    dob: str
    cdob: str
    ga: str
    chronoAge: str
    currentAgeMonths: str
    gender: str
    location: str
    deviceInfo: str
    uhid: str

class ReviewAction(BaseModel):
    uhid: str
    status: str
    comment: Optional[str] = ""


async def sync_data_from_drive():
    if not drive_client:
        logger.warning("DriveClient not initialized, skipping sync task.")
        return
    while True:
        try:
            logger.info("Starting data sync from Google Drive...")
            drive_data = drive_client.get_all_data()
            
            local_df = pd.read_csv(DATA_DB)
            local_uhids = set(local_df['Folder_Name'].tolist())
            
            new_records = []
            for record in drive_data:
                folder_name = record['folder_name']
                uhid = folder_name.replace("Data", "UHID")
                if uhid not in local_uhids:
                    json_details = record['info']['content']
                    new_records.append({
                        "Folder_Name": uhid,
                        "JSON_Details": json.dumps(json_details),
                        "VideoQ_status": json_details.get("VideoQ_status", "NA"),
                        "GMAE_status": json_details.get("GMAE_status", "Pending")
                    })
            
            if new_records:
                new_df = pd.DataFrame(new_records)
                new_df.to_csv(DATA_DB, mode='a', header=False, index=False)
                logger.info(f"Added {len(new_records)} new records from Drive to data.csv")
            
        except Exception as e:
            logger.error(f"Error during data sync: {e}")
        
        await asyncio.sleep(5)

@app.on_event("startup")
async def startup_event():
    if drive_client:
        asyncio.create_task(sync_data_from_drive())

# API Endpoints
@app.post("/api/login")
async def login(user: User):
    with open(USERS_DB, "r") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            if row[0] == user.email and row[1] == user.password:
                return {"message": "Login successful!"}
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

@app.post("/api/signup")
async def signup(user: UserSignup):
    with open(USERS_DB, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([user.email, user.password, "expert"])
    return {"message": "User created successfully!"}

@app.get("/api/dashboard-stats")
async def get_dashboard_stats():
    df = pd.read_csv(DATA_DB)
    approved_count = len(df[df['GMAE_status'] == 'Approved'])
    pending_count = len(df[df['GMAE_status'] == 'Pending'])
    flagged_count = len(df[df['GMAE_status'] == 'Rejected'])
    today_assigned = len(df)
    return {
        "today_assigned": today_assigned,
        "pending_videos": pending_count,
        "flagged_videos": flagged_count,
        "approved_videos": approved_count,
    }

@app.get("/api/pending-videos")
async def get_pending_videos():
    try:
        df = pd.read_csv(DATA_DB)
        pending_df = df[df['GMAE_status'] == 'Pending']
        videos = []
        for _, row in pending_df.iterrows():
            json_details = json.loads(row['JSON_Details'])
            videos.append({
                "babyName": json_details.get("Name"),
                "age": json_details.get("Age"),
                "uhid": row['Folder_Name']
            })
        return videos
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/approved-videos")
async def get_approved_videos():
    try:
        df = pd.read_csv(DATA_DB)
        approved_df = df[df['GMAE_status'] == 'Approved']
        videos = []
        for _, row in approved_df.iterrows():
            json_details = json.loads(row['JSON_Details'])
            videos.append({
                "babyName": json_details.get("Name"),
                "age": json_details.get("Age"),
                "uhid": row['Folder_Name'],
                "dob": json_details.get("DOB"),
                "chronoAge": json_details.get("ChronoAge"),
                "gender": json_details.get("Gender"),
                "location": json_details.get("Location"),
                "videoUrl": f"/api/videos/{row['Folder_Name']}"
            })
        return videos
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/flagged-videos")
async def get_flagged_videos():
    try:
        df = pd.read_csv(DATA_DB)
        flagged_df = df[df['GMAE_status'] == 'Rejected']
        videos = []
        for _, row in flagged_df.iterrows():
            json_details = json.loads(row['JSON_Details'])
            videos.append({
                "babyName": json_details.get("Name"),
                "age": json_details.get("Age"),
                "uhid": row['Folder_Name'],
                "dob": json_details.get("DOB"),
                "chronoAge": json_details.get("ChronoAge"),
                "gender": json_details.get("Gender"),
                "location": json_details.get("Location"),
                "comment": json_details.get("Comments"),
                "videoUrl": f"/api/videos/{row['Folder_Name']}"
            })
        return videos
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/videos/{uhid}")
async def get_video(uhid: str):
    folder_name = uhid.replace("UHID", "Data")
    video_path = Path(CLIPS_CACHE_DIR) / folder_name / "Clip1.mp4"
    
    if not video_path.exists():
        if drive_client:
            drive_client.download_clips_for_folder(folder_name)
        if not video_path.exists():
            raise HTTPException(status_code=404, detail="Video not found")
    
    return FileResponse(str(video_path), media_type="video/mp4")

@app.get("/api/video-details/{uhid}")
async def get_video_details(uhid: str):
    try:
        df = pd.read_csv(DATA_DB)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Database not found")

    video_row = df[df['Folder_Name'] == uhid]
    if video_row.empty:
        raise HTTPException(status_code=404, detail="Video details not found")
    
    row = video_row.iloc[0]
    json_details = json.loads(row['JSON_Details'])
    response_data = {
        "babyName": json_details.get("Name"),
        "dob": json_details.get("DOB"),
        "cdob": json_details.get("CDoB"),
        "ga": json_details.get("GA"),
        "chronoAge": json_details.get("ChronoAge"),
        "currentAgeMonths": json_details.get("CurrentAgeMonths"),
        "gender": json_details.get("Gender"),
        "location": json_details.get("Location"),
        "deviceInfo": json_details.get("DeviceInfo"),
        "uhid": uhid,
        "status": row['GMAE_status'],
        "comment": json_details.get("Comments", "")
    }
    return response_data

@app.post("/api/update-status/{uhid}")
async def update_video_status(uhid: str, action: ReviewAction):
    try:
        df = pd.read_csv(DATA_DB)
        if uhid not in df['Folder_Name'].values:
            raise HTTPException(status_code=404, detail="Video not found")
        
        df.loc[df['Folder_Name'] == uhid, 'GMAE_status'] = action.status
        
        json_details_str = df.loc[df['Folder_Name'] == uhid, 'JSON_Details'].iloc[0]
        json_details = json.loads(json_details_str)
        json_details['GMAE_status'] = action.status
        json_details['Comments'] = action.comment
        df.loc[df['Folder_Name'] == uhid, 'JSON_Details'] = json.dumps(json_details)
        df.to_csv(DATA_DB, index=False)
        
        timestamp = datetime.now().isoformat()
        folder_name = uhid.replace("UHID", "Data")
        if drive_client:
            drive_client.update_verification_status(folder_name, action.status, action.comment, timestamp)
        return {"message": f"Status for {uhid} updated to {action.status}"}
    except Exception as e:
        logger.error(f"Error during status update: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to update video status: {e}")

@app.post("/api/upload-video")
async def upload_video_and_metadata(
    video: UploadFile = File(...),
    name: str = "Anonymous",
    dob: str = "NA",
    cdob: str = "NA",
    ga: str = "NA",
    chronoAge: str = "NA",
    currentAgeMonths: str = "NA",
    gender: str = "NA",
    location: str = "NA",
    deviceInfo: str = "NA",
    comment: str = ""
):
    if not drive_client:
        raise HTTPException(status_code=503, detail="Drive service unavailable")
    try:
        folder_name = drive_client.get_next_folder_name_from_drive()
        uhid = folder_name.replace("Data", "UHID")
        
        local_folder_path = TEMP_DIR / folder_name
        local_folder_path.mkdir(exist_ok=True)
        video_filename = "Clip1.mp4"
        video_path = local_folder_path / video_filename
        
        async with aiofiles.open(video_path, 'wb') as out_file:
            content = await video.read()
            await out_file.write(content)

        patient_data = {
            "Name": name, "DOB": dob, "CDoB": cdob, "GA": ga,
            "ChronoAge": chronoAge, "CurrentAgeMonths": currentAgeMonths,
            "Gender": gender, "Location": location, "DeviceInfo": deviceInfo,
            "Comments": comment, "GMAE_status": "Pending",
            "VideoQ_status": "NA", "Folder_Name": folder_name
        }
        patient_txt_path = local_folder_path / "patient.txt"
        with open(patient_txt_path, 'w') as f:
            for key, value in patient_data.items():
                f.write(f"{key}: {value}\n")

        drive_client.upload_to_drive(folder_name, "Clips", str(video_path), file_name=video_filename)
        drive_client.upload_to_drive(folder_name, "", str(patient_txt_path))

        df = pd.read_csv(DATA_DB)
        new_row = {
            "Folder_Name": uhid,
            "JSON_Details": json.dumps(patient_data),
            "VideoQ_status": "NA",
            "GMAE_status": "Pending"
        }
        pd.DataFrame([new_row]).to_csv(DATA_DB, mode='a', header=False, index=False)
        
        shutil.rmtree(local_folder_path)

        return {"message": f"Video for {name} uploaded successfully with UHID: {uhid}"}

    except Exception as e:
        logger.error(f"Error during video upload: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to upload video: {e}")