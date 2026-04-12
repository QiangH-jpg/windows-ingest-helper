import os
from tos import TosClientV2

TOS_AK = os.getenv('TOS_AK')
TOS_SK = os.getenv('TOS_SK')

if not TOS_AK or not TOS_SK:
    print('Error: TOS secrets not configured')
    exit(1)

TOS_BUCKET = 'e23-video'
TOS_REGION = 'cn-beijing'
TOS_ENDPOINT = f'tos-{TOS_REGION}.volces.com'

print('TOS secrets configured')
print(f'Bucket: {TOS_BUCKET}')
print(f'Region: {TOS_REGION}')

client = TosClientV2(ak=TOS_AK, sk=TOS_SK, endpoint=TOS_ENDPOINT, region=TOS_REGION)

zip_path = 'Windows_Ingest_Helper_v7.zip'
tos_key = 'Windows_Executable/Windows_Ingest_Helper_v7.zip'

print(f'\nUploading {zip_path} to {tos_key}...')
print(f'File exists: {os.path.exists(zip_path)}')

try:
    client.put_object_from_file(bucket=TOS_BUCKET, key=tos_key, file_path=zip_path)
    print('Upload successful')
    print(f'Download URL: https://{TOS_BUCKET}.{TOS_ENDPOINT}/{tos_key}')
except Exception as e:
    print(f'Upload failed: {e}')
    exit(1)
