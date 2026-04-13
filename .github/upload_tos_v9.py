import os
from tos import TosClientV2

TOS_AK = os.getenv("TOS_AK")
TOS_SK = os.getenv("TOS_SK")

if not TOS_AK or not TOS_SK:
    print("Error: TOS secrets not configured")
    exit(1)

TOS_BUCKET = "e23-video"
TOS_REGION = "cn-beijing"
TOS_ENDPOINT = f"tos-{TOS_REGION}.volces.com"

client = TosClientV2(ak=TOS_AK, sk=TOS_SK, endpoint=TOS_ENDPOINT, region=TOS_REGION)

zip_path = "Windows_Ingest_Helper_v9.zip"
tos_key = os.getenv("TOS_KEY", "Windows_Executable/Windows_Ingest_Helper_v9.zip")

print(f"Uploading {zip_path} to {tos_key}...")
client.put_object_from_file(bucket=TOS_BUCKET, key=tos_key, file_path=zip_path)
print("Upload successful")
print(f"Download URL: https://{TOS_BUCKET}.{TOS_ENDPOINT}/{tos_key}")
