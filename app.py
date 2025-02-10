import io
import os
from typing import Optional, Tuple

import boto3
from chalice import Chalice
from PIL import Image

app = Chalice(app_name='s3-compress')

class Configuration:
    """Load configuration from environment variables"""
    def __init__(self):
        self.bucket = os.environ.get('S3_BUCKET')
        self.region = os.environ.get('S3_REGION')
        self.output_bucket = os.environ.get('OUTPUT_BUCKET')
        self.output_directory = os.environ.get('OUTPUT_DIRECTORY', 'compressed/')
        self.prefix = os.environ.get('INPUT_PREFIX', 'profile/')
        self.max_size = (
            int(os.environ.get('MAX_IMAGE_SIZE', '800')),
            int(os.environ.get('MAX_IMAGE_SIZE', '800'))
        )
        self.quality = int(os.environ.get('COMPRESSION_QUALITY', '85'))
        self.supported_formats = ['.jpg', '.jpeg', '.png']
        
        # Validate required configuration
        self._validate_config()
    
    def _validate_config(self):
        required_vars = ['S3_BUCKET', 'S3_REGION', 'OUTPUT_BUCKET']
        missing_vars = [var for var in required_vars if not os.environ.get(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Initialize configuration
config = Configuration()

class CompressionHandler:
    def __init__(self):
        self.s3_client = boto3.client('s3', region_name=config.region)
    
    def get_file_extension(self, filename: str) -> str:
        return os.path.splitext(filename.lower())[1]
    
    def is_supported_format(self, filename: str) -> bool:
        return self.get_file_extension(filename) in config.supported_formats
    
    def download_image(self, bucket: str, key: str) -> Optional[bytes]:
        """Download image from S3."""
        try:
            response = self.s3_client.get_object(Bucket=bucket, Key=key)
            return response['Body'].read()
        except Exception as e:
            app.log.error(f"Error downloading image {key} from {bucket}: {str(e)}")
            return None
    
    def compress_image(self, image_data: bytes) -> Tuple[Optional[bytes], str]:
        try:
            # Open image using PIL
            img = Image.open(io.BytesIO(image_data))
            
            # Convert RGBA to RGB if necessary
            if img.mode in ('RGBA', 'LA'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[-1])
                img = background
            
            # Resize if larger than max_size while maintaining aspect ratio
            img.thumbnail(config.max_size, Image.Resampling.LANCZOS)
            
            # Save compressed image to bytes buffer
            output_buffer = io.BytesIO()
            img.save(output_buffer, format='JPEG', quality=config.quality, 
                    optimize=True)
            
            return output_buffer.getvalue(), 'image/jpeg'
        except Exception as e:
            app.log.error(f"Error compressing image: {str(e)}")
            return None, ''
    
    def upload_compressed(self, image_data: bytes, content_type: str, 
                         original_key: str) -> bool:
        try:
            filename = os.path.basename(original_key)
            new_key = f"{config.output_directory}{filename}"
            if not new_key.lower().endswith('.jpg'):
                new_key = f"{new_key.rsplit('.', 1)[0]}.jpg"
            
            self.s3_client.put_object(
                Bucket=config.output_bucket,
                Key=new_key,
                Body=image_data,
                ContentType=content_type,
                Metadata={
                    'original-key': original_key,
                    'compressed': 'true'
                }
            )
            return True
        except Exception as e:
            app.log.error(f"Error uploading compressed image: {str(e)}")
            return False

@app.route('/')
def index():
    return {
        'hello': 'world',
        'config': {
            'bucket': config.bucket,
            'region': config.region,
            'output_directory': config.output_directory,
            'prefix': config.prefix
        }
    }

@app.on_s3_event(bucket=config.bucket or "",
                 events=['s3:ObjectCreated:*'],
                 prefix=config.prefix)
def handle_s3_event(event):
    app.log.debug("Received event for bucket: %s, key: %s",
                  event.bucket, event.key)
    
    handler = CompressionHandler()
    
    if not handler.is_supported_format(event.key):
        app.log.info(f"Skipping unsupported format: {event.key}")
        return
    
    image_data = handler.download_image(event.bucket, event.key)
    if not image_data:
        app.log.error(f"Failed to download image: {event.key}")
        return
    
    compressed_data, content_type = handler.compress_image(image_data)
    if not compressed_data:
        app.log.error(f"Failed to compress image: {event.key}")
        return
    
    if handler.upload_compressed(compressed_data, content_type, event.key):
        app.log.info(f"Successfully compressed and uploaded: {event.key}")
    else:
        app.log.error(f"Failed to upload compressed image: {event.key}")