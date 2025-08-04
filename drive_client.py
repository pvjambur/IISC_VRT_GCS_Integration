import os
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from googleapiclient.errors import HttpError
import logging
from pathlib import Path
import time
import re
import shutil
import json
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DriveClient:
    def __init__(self):
        self.drive = self._initialize_drive()
        self.root_folder_id = 'root'
        self.local_base = "static/temp"
        Path(self.local_base).mkdir(parents=True, exist_ok=True)
        self.drive_space = self.get_drive_space()
        self.local_folders = self._scan_local_folders()

    def _initialize_drive(self):
        try:
            gauth = GoogleAuth()
            gauth.LoadCredentialsFile("credentials.json")
            if gauth.credentials is None:
                gauth.LocalWebserverAuth()
            elif gauth.access_token_expired:
                gauth.Refresh()
            else:
                gauth.Authorize()
            gauth.SaveCredentialsFile("credentials.json")
            return GoogleDrive(gauth)
        except Exception as e:
            logger.error(f"Error initializing drive: {e}")
            if not Path("credentials.json").exists():
                logger.info("Trying to authenticate using settings.yaml")
                gauth = GoogleAuth(settings_file='settings.yaml')
                gauth.LocalWebserverAuth()
                gauth.SaveCredentialsFile("credentials.json")
                return GoogleDrive(gauth)
            raise

    def _scan_local_folders(self):
        folders = {}
        for folder in Path(self.local_base).iterdir():
            if folder.is_dir() and folder.name.startswith("Data"):
                folders[folder.name] = {
                    'path': str(folder),
                    'files': [f.name for f in folder.iterdir() if f.is_file()]
                }
        return folders

    def get_drive_space(self):
        try:
            about = self.drive.auth.service.about().get(fields="storageQuota").execute()
            used = int(about['storageQuota']['usage']) / (1024**3)
            total = int(about['storageQuota']['limit']) / (1024**3)
            return {
                'used': round(used, 2),
                'total': round(total, 2),
                'free': round(total - used, 2),
                'percentage': round((used / total) * 100, 2)
            }
        except Exception as e:
            logger.error(f"Error getting drive space: {e}")
            return {'used': 0, 'total': 0, 'free': 0, 'percentage': 0}

    def get_next_folder_name(self):
        existing = [f for f in Path(self.local_base).iterdir() if f.is_dir() and f.name.startswith("Data")]
        nums = []
        for folder in existing:
            match = re.match(r'Data(\d+)', folder.name)
            if match:
                nums.append(int(match.group(1)))
        next_num = max(nums) + 1 if nums else 1
        return f"Data{next_num}"

    def create_local_folder(self, folder_name=None):
        if not folder_name:
            folder_name = self.get_next_folder_name()
        folder_path = Path(self.local_base) / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)
        self.local_folders[folder_name] = {
            'path': str(folder_path),
            'files': []
        }
        return folder_name, str(folder_path)

    def upload_to_drive(self, folder_name, file_path, file_name=None):
        try:
            if not file_name:
                file_name = os.path.basename(file_path)
            
            # Find or create folder in Drive
            query = f"title='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            folders = self.drive.ListFile({'q': query}).GetList()
            
            if not folders:
                folder_metadata = {
                    'title': folder_name,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [{'id': self.root_folder_id}]
                }
                folder = self.drive.CreateFile(folder_metadata)
                folder.Upload()
                folder_id = folder['id']
            else:
                folder_id = folders[0]['id']
            
            # Upload file
            file_metadata = {
                'title': file_name,
                'parents': [{'id': folder_id}]
            }
            file = self.drive.CreateFile(file_metadata)
            file.SetContentFile(file_path)
            file.Upload()
            
            # Update drive space
            self.drive_space = self.get_drive_space()
            
            return file['id']
        except Exception as e:
            logger.error(f"Error uploading to drive: {e}")
            raise

    def get_all_data(self):
        data = []
        for folder_name, folder_info in self.local_folders.items():
            folder_path = Path(folder_info['path'])
            info_file = None
            video_file = None
            
            for file in folder_path.iterdir():
                if file.name.endswith('.txt') and 'info' in file.name.lower():
                    with open(file, 'r') as f:
                        info_content = f.read()
                    info_file = {
                        'name': file.name,
                        'content': info_content,
                        'path': str(file)
                    }
                elif file.name.lower().endswith(('.mp4', '.mov', '.avi', '.mkv')):
                    video_file = {
                        'name': file.name,
                        'path': str(file)
                    }
            
            if info_file and video_file:
                data.append({
                    'folder': folder_name,
                    'info': info_file,
                    'video': video_file
                })
        return data