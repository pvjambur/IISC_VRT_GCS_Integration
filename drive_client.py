import os
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import logging
from pathlib import Path
import re
import shutil
import json
from typing import Dict, List, Optional
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DriveClient:
    def __init__(self):
        self._hardcoded_space = {
            'used': 0.0,
            'total': 15.0,
            'free': 15.0,
            'percentage': 0.0
        }
        self.drive = self._initialize_drive()
        self.root_folder_id = 'root'
        self.local_base = "static/temp"
        self.clips_cache = "static/clips_cache"
        Path(self.local_base).mkdir(parents=True, exist_ok=True)
        Path(self.clips_cache).mkdir(exist_ok=True)
        self.local_folders = self._scan_local_folders()

    def _initialize_drive(self):
        try:
            gauth = GoogleAuth()
            if Path("credentials.json").exists():
                gauth.LoadCredentialsFile("credentials.json")
            if gauth.credentials is None:
                gauth.LocalWebserverAuth()
            elif gauth.access_token_expired:
                gauth.Refresh()
            else:
                gauth.Authorize()
            gauth.SaveCredentialsFile("credentials.json")
            self.drive_service = build("drive", "v3", http=gauth.http)
            return GoogleDrive(gauth)
        except HttpError as error:
            logger.error(f"HTTP Error during Drive initialization: {error}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error initializing drive: {e}")
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
        return self._hardcoded_space
    
    def increment_drive_space(self, size_in_bytes: int):
        size_in_gb = size_in_bytes / (1024**3)
        self._hardcoded_space['used'] += size_in_gb
        self._hardcoded_space['free'] -= size_in_gb
        
        if self._hardcoded_space['total'] > 0:
            self._hardcoded_space['percentage'] = round((self._hardcoded_space['used'] / self._hardcoded_space['total']) * 100, 2)
        
        self._hardcoded_space['used'] = round(self._hardcoded_space['used'], 2)
        self._hardcoded_space['free'] = round(self._hardcoded_space['free'], 2)
        
        logger.info(f"Drive space updated: {self._hardcoded_space}")

    def get_next_folder_name_from_drive(self):
        try:
            folders = self.drive.ListFile({'q': "'root' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"}).GetList()
            nums = [int(re.match(r'Data(\d+)', f['title']).group(1)) for f in folders if f['title'].startswith("Data")]
            next_num = max(nums) + 1 if nums else 1
            return f"Data{next_num}"
        except Exception as e:
            logger.error(f"Error getting next folder name from drive: {e}")
            return self.get_next_folder_name_from_local()
    
    def get_next_folder_name_from_local(self):
        existing_folders = [d for d in Path(self.local_base).iterdir() if d.is_dir() and d.name.startswith("Data")]
        numbers = [int(f.name[4:]) for f in existing_folders]
        next_num = max(numbers) + 1 if numbers else 1
        return f"Data{next_num}"

    def get_folder_id(self, folder_name: str):
        query = f"title='{folder_name}' and 'root' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders = self.drive.ListFile({'q': query}).GetList()
        return folders[0]['id'] if folders else None

    def create_drive_folder(self, folder_name: str):
        folder_id = self.get_folder_id(folder_name)
        if not folder_id:
            folder_metadata = {
                'title': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [{'id': 'root'}]
            }
            folder = self.drive.CreateFile(folder_metadata)
            folder.Upload()
            logger.info(f"Created folder '{folder_name}' in drive")
            return folder['id']
        return folder_id

    def upload_to_drive(self, folder_name, subfolder_name, file_path, file_name=None):
        try:
            folder_id = self.create_drive_folder(folder_name)
            subfolder_id = self.create_drive_subfolder(folder_id, subfolder_name)
            
            if not file_name:
                file_name = os.path.basename(file_path)
            
            query = f"title='{file_name}' and '{subfolder_id}' in parents and trashed=false"
            existing_files = self.drive.ListFile({'q': query}).GetList()
            if existing_files:
                logger.info(f"File '{file_name}' already exists in '{subfolder_name}', skipping upload")
                return existing_files[0]['id']
            
            file_metadata = {
                'title': file_name,
                'parents': [{'id': subfolder_id}]
            }
            
            file = self.drive.CreateFile(file_metadata)
            file.SetContentFile(file_path)
            file.Upload()
            
            file_size_bytes = os.path.getsize(file_path)
            self.increment_drive_space(file_size_bytes)
            
            logger.info(f"Successfully uploaded '{file_name}' to '{folder_name}/{subfolder_name}'")
            return file['id']
        except Exception as e:
            logger.error(f"Error uploading to drive: {e}")
            raise

    def get_all_data(self):
        data = []
        try:
            for folder in self.drive.ListFile({'q': "'root' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"}).GetList():
                if not folder['title'].startswith('Data'):
                    continue
                
                folder_name = folder['title']
                folder_id = folder['id']
                
                info_file = None
                
                info_query = f"title='patient.txt' and '{folder_id}' in parents and trashed=false"
                info_files = self.drive.ListFile({'q': info_query}).GetList()
                if info_files:
                    info_content = info_files[0].GetContentString()
                    info_dict = {}
                    for line in info_content.split('\n'):
                        if ': ' in line:
                            key, value = line.split(': ', 1)
                            info_dict[key.strip()] = value.strip()
                    info_file = {'name': info_files[0]['title'], 'content': info_dict}
                    
                    data.append({
                        'folder_name': folder_name,
                        'info': info_file,
                        'status': info_dict.get('GMAE_status', 'Pending'),
                        'comment': info_dict.get('Comments', '')
                    })
        except Exception as e:
            logger.error(f"Error getting all data from drive: {e}")
            return []
        return data

    def get_folder_details(self, folder_name: str) -> Optional[Dict]:
        folder_id = self.get_folder_id(folder_name)
        if not folder_id:
            return None
        
        info_query = f"title='patient.txt' and '{folder_id}' in parents and trashed=false"
        info_files = self.drive.ListFile({'q': info_query}).GetList()
        
        if not info_files:
            return None
            
        info_content = info_files[0].GetContentString()
        info_dict = {}
        for line in info_content.split('\n'):
            if ': ' in line:
                key, value = line.split(': ', 1)
                info_dict[key.strip()] = value.strip()
        
        return {'info': info_dict}

    def download_clips_for_folder(self, folder_name: str):
        clips_drive_path = f"{folder_name}/Clips"
        clips_folder_id = self.get_folder_id(clips_drive_path)
        
        if not clips_folder_id:
            logger.error(f"Clips folder '{clips_drive_path}' not found on Drive.")
            return None
            
        local_clips_dir = Path(self.clips_cache) / folder_name
        local_clips_dir.mkdir(parents=True, exist_ok=True)
        
        query = f"'{clips_folder_id}' in parents and trashed=false"
        clips = self.drive.ListFile({'q': query}).GetList()
        
        # Download all clips in the folder
        for clip in clips:
            local_path = local_clips_dir / clip['title']
            if not local_path.exists():
                logger.info(f"Downloading clip: {clip['title']}")
                clip.GetContentFile(str(local_path))
        
        return local_clips_dir

    def update_verification_status(self, folder_name, status, reason, timestamp):
        folder_id = self.get_folder_id(folder_name)
        if not folder_id:
            raise Exception("Folder not found on Drive.")
        
        query = f"title='patient.txt' and '{folder_id}' in parents and trashed=false"
        info_files = self.drive.ListFile({'q': query}).GetList()
        if not info_files:
            raise Exception("Patient info file not found.")

        info_file = info_files[0]
        content = info_file.GetContentString()
        
        lines = content.split('\n')
        new_lines = []
        
        def update_line(key, value, current_lines):
            found = False
            updated_lines = []
            for line in current_lines:
                if line.startswith(f"{key}:"):
                    updated_lines.append(f"{key}: {value}")
                    found = True
                else:
                    updated_lines.append(line)
            if not found:
                updated_lines.append(f"{key}: {value}")
            return updated_lines

        new_lines = update_line("GMAE_status", status, lines)
        new_lines = update_line("Comments", reason, new_lines)
        new_lines = update_line("Timestamp", timestamp or '', new_lines)
        
        new_content = "\n".join(new_lines)
        
        info_file.SetContentString(new_content)
        info_file.Upload()
        logger.info(f"Updated verification status for {folder_name}: {status}")

    def create_drive_subfolder(self, parent_id, subfolder_name):
        query = f"title='{subfolder_name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
        folders = self.drive.ListFile({'q': query}).GetList()
        if not folders:
            folder_metadata = {
                'title': subfolder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [{'id': parent_id}]
            }
            folder = self.drive.CreateFile(folder_metadata)
            folder.Upload()
            logger.info(f"Created subfolder '{subfolder_name}' in drive")
            return folder['id']
        return folders[0]['id'] 