import os
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
import logging
from pathlib import Path
import re
import shutil
import json
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DriveClient:
    def __init__(self):
        # Hardcoded initial drive space
        self._hardcoded_space = {
            'used': 0.0,
            'total': 15.0,
            'free': 15.0,
            'percentage': 0.0
        }
        
        self.drive = self._initialize_drive()
        self.root_folder_id = 'root'
        self.local_base = "static/temp"
        Path(self.local_base).mkdir(parents=True, exist_ok=True)
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
            
            self.drive_service = build("drive", "v3", http=gauth.http)
            return GoogleDrive(gauth)
        except Exception as e:
            logger.error(f"Error initializing drive: {e}")
            if not Path("credentials.json").exists():
                logger.info("Trying to authenticate using settings.yaml")
                gauth = GoogleAuth(settings_file='settings.yaml')
                gauth.LocalWebserverAuth()
                gauth.SaveCredentialsFile("credentials.json")
                
                self.drive_service = build("drive", "v3", http=gauth.http)
                
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
    
    def create_local_folder(self):
        folder_name = self.get_next_folder_name()
        folder_path = Path(self.local_base) / folder_name
        folder_path.mkdir(exist_ok=True)
        return folder_name, folder_path

    def get_drive_space(self):
        # Return the hardcoded values instead of making an API call
        return self._hardcoded_space
    
    def increment_drive_space(self, size_in_bytes: int):
        """Increments the hardcoded drive space with the new file size."""
        size_in_gb = size_in_bytes / (1024**3)
        self._hardcoded_space['used'] += size_in_gb
        self._hardcoded_space['free'] -= size_in_gb
        
        # Recalculate the percentage
        if self._hardcoded_space['total'] > 0:
            self._hardcoded_space['percentage'] = round((self._hardcoded_space['used'] / self._hardcoded_space['total']) * 100, 2)
        
        # Round the values for display
        self._hardcoded_space['used'] = round(self._hardcoded_space['used'], 2)
        self._hardcoded_space['free'] = round(self._hardcoded_space['free'], 2)
        
        logger.info(f"Drive space updated: {self._hardcoded_space}")

    def get_next_folder_name(self):
        folders = self.drive.ListFile({'q': "'root' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"}).GetList()
        nums = []
        for folder in folders:
            if folder['title'].startswith("Data"):
                match = re.match(r'Data(\d+)', folder['title'])
                if match:
                    nums.append(int(match.group(1)))
        
        next_num = max(nums) + 1 if nums else 1
        return f"Data{next_num}"

    def get_folder_id(self, folder_path: str):
        path_parts = folder_path.split('/')
        parent_id = 'root'
        for part in path_parts:
            query = f"title='{part}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            folders = self.drive.ListFile({'q': query}).GetList()
            if not folders:
                return None
            parent_id = folders[0]['id']
        return parent_id

    def create_drive_folder(self, folder_path: str):
        path_parts = folder_path.split('/')
        parent_id = 'root'
        for part in path_parts:
            query = f"title='{part}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
            folders = self.drive.ListFile({'q': query}).GetList()
            if not folders:
                folder_metadata = {
                    'title': part,
                    'mimeType': 'application/vnd.google-apps.folder',
                    'parents': [{'id': parent_id}]
                }
                folder = self.drive.CreateFile(folder_metadata)
                folder.Upload()
                parent_id = folder['id']
            else:
                parent_id = folders[0]['id']
        return parent_id

    def upload_to_drive(self, drive_path, file_path, file_name=None):
        try:
            folder_id = self.create_drive_folder(drive_path)
            
            if not file_name:
                file_name = os.path.basename(file_path)
            
            file_metadata = {
                'title': file_name,
                'parents': [{'id': folder_id}]
            }
            
            file = self.drive.CreateFile(file_metadata)
            file.SetContentFile(file_path)
            file.Upload()
            
            # Since we're using hardcoded space, we increment it here
            file_size_bytes = os.path.getsize(file_path)
            self.increment_drive_space(file_size_bytes)
            
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
                video_file = None
                status = "Pending"
                reason = ""
                
                info_query = f"title='patient.txt' and '{folder_id}' in parents and trashed=false"
                info_files = self.drive.ListFile({'q': info_query}).GetList()
                if info_files:
                    info_content = info_files[0].GetContentString()
                    info_dict = {}
                    for line in info_content.split('\n'):
                        if ': ' in line:
                            key, value = line.split(': ', 1)
                            info_dict[key.strip()] = value.strip()
                    
                    info_file = {
                        'name': info_files[0]['title'],
                        'content': info_dict
                    }
                    status = info_dict.get('Verification Status', 'Pending')
                    reason = info_dict.get('Reason', '')
                
                video_query = f"title contains 'original_video' and '{folder_id}' in parents and trashed=false"
                video_files = self.drive.ListFile({'q': video_query}).GetList()
                if video_files:
                    video_file = {
                        'name': video_files[0]['title'],
                        'id': video_files[0]['id']
                    }

                if info_file:
                    data.append({
                        'folder': folder_name,
                        'info': info_file,
                        'video': video_file,
                        'status': status,
                        'reason': reason
                    })
        except Exception as e:
            logger.error(f"Error getting all data from drive: {e}")
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
            
        local_clips_dir = Path("static/clips_cache") / folder_name
        local_clips_dir.mkdir(parents=True, exist_ok=True)
        
        query = f"'{clips_folder_id}' in parents and trashed=false"
        clips = self.drive.ListFile({'q': query}).GetList()
        
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
        for line in lines:
            if line.startswith("Verification Status:"):
                new_lines.append(f"Verification Status: {status}")
            elif line.startswith("Reason:"):
                new_lines.append(f"Reason: {reason}")
            elif line.startswith("Timestamp:"):
                new_lines.append(f"Timestamp: {timestamp or ''}")
            else:
                new_lines.append(line)
        
        new_content = "\n".join(new_lines)
        
        info_file.SetContentString(new_content)
        info_file.Upload()