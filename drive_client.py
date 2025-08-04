import os
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from googleapiclient.errors import HttpError
import logging
from pathlib import Path
import time
import threading
import re
from typing import Dict, List
from datetime import datetime
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class DriveClient:
    def __init__(self):
        self.drive = self._initialize_drive()
        self.root_folder_id = 'root'
        self.local_temp = "static/temp"
        Path(self.local_temp).mkdir(parents=True, exist_ok=True)
        self.upload_progress = {}
        self.drive_space = self.get_drive_space()

    def _initialize_drive(self):
        try:
            gauth = GoogleAuth()
            # Try to load saved credentials
            gauth.LoadCredentialsFile("credentials.json")
            if gauth.credentials is None:
                # Authenticate if they're not there
                gauth.LocalWebserverAuth()
            elif gauth.access_token_expired:
                # Refresh them if expired
                gauth.Refresh()
            else:
                # Initialize the saved creds
                gauth.Authorize()
            # Save the current credentials to a file
            gauth.SaveCredentialsFile("credentials.json")
            return GoogleDrive(gauth)
        except Exception as e:
            logger.error(f"Error initializing drive: {e}")
            # Fall back to settings.yaml if credentials.json doesn't exist
            if not Path("credentials.json").exists():
                logger.info("Trying to authenticate using settings.yaml")
                gauth = GoogleAuth(settings_file='settings.yaml')
                gauth.LocalWebserverAuth()
                gauth.SaveCredentialsFile("credentials.json")
                return GoogleDrive(gauth)
            raise

    def get_drive_space(self):
        try:
            about = self.drive.auth.service.about().get(fields="storageQuota").execute()
            used = int(about['storageQuota']['usage']) / (1024**3)  # in GB
            total = int(about['storageQuota']['limit']) / (1024**3)  # in GB
            return {
                'used': round(used, 2),
                'total': round(total, 2),
                'free': round(total - used, 2),
                'percentage': round((used / total) * 100, 2)
            }
        except Exception as e:
            logger.error(f"Error getting drive space: {e}")
            return {
                'used': 0,
                'total': 0,
                'free': 0,
                'percentage': 0
            }

    def create_data_folder(self, base_name="Data"):
        try:
            # Check for existing folders with the same base name
            query = f"title contains '{base_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{self.root_folder_id}' in parents"
            existing_folders = self.drive.ListFile({'q': query}).GetList()
            
            # Determine the next available folder name
            max_num = 0
            for folder in existing_folders:
                title = folder['title']
                if title == base_name:
                    max_num = max(max_num, 1)
                elif match := re.match(rf"{base_name}(\d+)", title):
                    num = int(match.group(1))
                    max_num = max(max_num, num)
                elif match := re.match(rf"{base_name}(\d+)_\d+", title):
                    num = int(match.group(1))
                    max_num = max(max_num, num)
            
            new_num = max_num + 1
            folder_name = f"{base_name}{new_num}"
            
            # Check if this name already exists (with suffix)
            suffix = 1
            while any(f['title'] == folder_name for f in existing_folders):
                folder_name = f"{base_name}{new_num}_{suffix}"
                suffix += 1
            
            # Create the folder
            folder_metadata = {
                'title': folder_name,
                'mimeType': 'application/vnd.google-apps.folder',
                'parents': [{'id': self.root_folder_id}]
            }
            folder = self.drive.CreateFile(folder_metadata)
            folder.Upload()
            
            logger.info(f"Created new folder: {folder_name}")
            return folder['id']
        except Exception as e:
            logger.error(f"Error creating folder: {e}")
            raise

    def upload_file_to_folder(self, folder_id, file_path, file_name=None):
        try:
            if file_name is None:
                file_name = os.path.basename(file_path)
            
            file_metadata = {
                'title': file_name,
                'parents': [{'id': folder_id}]
            }
            
            file = self.drive.CreateFile(file_metadata)
            file.SetContentFile(file_path)
            
            # Track upload progress
            self.upload_progress[file_name] = 0
            
            def upload_progress_callback(progress):
                self.upload_progress[file_name] = progress
            
            file.Upload({'progress_callback': upload_progress_callback})
            
            # Update drive space after upload
            self.drive_space = self.get_drive_space()
            
            logger.info(f"Uploaded {file_name} to folder {folder_id}")
            return file['id']
        except Exception as e:
            logger.error(f"Error uploading file: {e}")
            raise

    def get_upload_progress(self, file_name):
        return self.upload_progress.get(file_name, 0)