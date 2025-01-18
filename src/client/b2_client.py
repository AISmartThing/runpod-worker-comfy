import logging
from typing import Optional
from b2sdk.v2 import B2Api, InMemoryAccountInfo

class B2Client:
    _instance = None
    
    @classmethod
    def get_instance(cls):
        """
        Get or create a singleton instance of B2Client
        """
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        """
        Initialize B2 client using credentials from environment variables
        """
        application_key_id = "0057f5868c855860000000001"
        application_key = "K005ZCmscue4fX4XuPwNbHyhARHkIu4"
        bucket_name = "blue-rio"
        
        if not all([application_key_id, application_key, bucket_name]):
            raise ValueError("B2 credentials not found in environment variables")
            
        self.info = InMemoryAccountInfo()
        self.api = B2Api(self.info)
        self.api.authorize_account("production", application_key_id, application_key)
        
        self.bucket = self.api.get_bucket_by_name(bucket_name)
        
    def upload_file(self, file_path: str, b2_path: str) -> bool:
        """
        Upload a file to B2
        
        Args:
            file_path: Local path to the file
            b2_path: Destination path in B2
            
        Returns:
            bool: True if upload was successful, False otherwise
        """
        try:
            self.bucket.upload_local_file(
                local_file=file_path,
                file_name=b2_path
            )
            logging.info(f"Successfully uploaded {file_path} to {b2_path}")
            return True
        except Exception as e:
            logging.error(f"Failed to upload {file_path}: {str(e)}")
            return False
            
    def download_file(self, b2_path: str, local_path: str) -> bool:
        """
        Download a file from B2
        
        Args:
            b2_path: Path of the file in B2
            local_path: Local path where the file should be saved
            
        Returns:
            bool: True if download was successful, False otherwise
        """
        try:
            downloaded_file = self.bucket.download_file_by_name(b2_path)
            downloaded_file.save_to(local_path)
            logging.info(f"Successfully downloaded {b2_path} to {local_path}")
            return True
        except Exception as e:
            logging.error(f"Failed to download {b2_path}: {str(e)}")
            return False
            
    def generate_download_url(self, b2_path: str, duration_in_seconds: int = 3600) -> Optional[str]:
        """
        Generate a download URL for a file
        
        Args:
            b2_path: Path of the file in B2
            duration_in_seconds: Time in seconds until the URL expires (default: 1 hour)
            
        Returns:
            str: Download URL if successful, None otherwise
        """
        try:
            file_info = self.bucket.get_file_info_by_name(b2_path)
            url = self.bucket.get_download_url(file_info.id_)
            logging.info(f"Generated download URL for {b2_path}")
            return url
        except Exception as e:
            logging.error(f"Failed to generate download URL for {b2_path}: {str(e)}")
            return None
