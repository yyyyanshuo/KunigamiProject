from core.runtime import LOCAL_TEST, load_environment
# -*- coding: utf-8 -*-
import os
from qcloud_cos import CosConfig
from qcloud_cos import CosS3Client

import mimetypes

load_environment()

# 1. 初始化配置
secret_id = os.getenv('COS_SECRET_ID')
secret_key = os.getenv('COS_SECRET_KEY')
region = os.getenv('COS_REGION')
bucket = os.getenv('COS_BUCKET')

client = None
if not LOCAL_TEST and all((region, secret_id, secret_key, bucket)):
    config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
    client = CosS3Client(config)

def upload_to_cos(local_path, cos_path):
    """
    将本地文件上传到 COS
    local_path: 本地文件路径，例如 'static/uploads/abc.jpg'
    cos_path: 存储在COS中的路径，例如 'chat_images/abc.jpg'
    """
    if LOCAL_TEST:
        from core.local_storage import store_upload
        return store_upload(local_path, cos_path)
    if client is None:
        return None
    try:
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = 'application/octet-stream'

        with open(local_path, 'rb') as fp:
            response = client.put_object(
                Bucket=bucket,
                Body=fp,
                Key=cos_path,
                StorageClass='STANDARD',
                ContentType=content_type
            )
        # 拼接并返回公网访问 URL
        return f"https://{bucket}.cos.{region}.myqcloud.com/{cos_path}"
    except Exception as e:
        print(f"❌ COS 上传失败: {e}")
        return None


def download_from_cos(cos_path):
    """Read one COS object and return its bytes, or None when it is unavailable."""
    object_key = str(cos_path or "").lstrip("/")
    if client is None or not bucket or not object_key:
        return None
    try:
        response = client.get_object(Bucket=bucket, Key=object_key)
        body = response.get("Body")
        if body is None:
            return None
        return body.get_raw_stream().read()
    except Exception as e:
        print(f"❌ COS 下载失败 ({object_key}): {e}")
        return None


def delete_from_cos(cos_path):
    """Delete one exact COS object key. Returns True when the request succeeds."""
    if client is None or not bucket or not region or not cos_path:
        return False
    try:
        client.delete_object(Bucket=bucket, Key=cos_path)
        return True
    except Exception as e:
        print(f"❌ COS 删除失败: {e}")
        return False

def get_cos_list(prefix, get_folders=False):
    """
    prefix: 路径前缀，例如 'stickers/'
    get_folders: True则列出子文件夹名，False则列出文件名
    """
    if client is None:
        return []
    files = []
    folders = []

    # 调用腾讯云接口
    response = client.list_objects(
        Bucket=bucket,
        Prefix=prefix,
        Delimiter='/' # 这个斜杠是模拟文件夹的关键
    )

    # 1. 获取模拟的“子文件夹”（比如：线条小狗/）
    if get_folders and 'CommonPrefixes' in response:
        for item in response['CommonPrefixes']:
            # 只要文件夹名字
            folder_name = item['Prefix'].replace(prefix, "").strip('/')
            folders.append(folder_name)
        return folders

    # 2. 获取里面的文件（比如：OK.gif）
    if 'Contents' in response:
        for item in response['Contents']:
            key = item['Key']
            if key != prefix: # 排除掉目录本身
                # 拼接完整URL
                url = f"https://{bucket}.cos.{region}.myqcloud.com/{key}"
                files.append({
                    "name": os.path.basename(key), # 文件名
                    "url": url # 完整链接
                })
        return files

    return []


def delete_user_objects(user_id):
    """Delete one private user prefix, including versions; fail on cloud errors.

    Called only after password AND typed-phrase confirmation at account deletion.
    Never touches the shared square/ or stickers/ prefixes. Local mode is a no-op.
    """
    uid = int(user_id)
    if uid <= 0:
        raise ValueError('Invalid user ID')
    if LOCAL_TEST:
        return
    if client is None:
        if any((region, bucket, secret_id, secret_key)):
            raise RuntimeError('Cloud storage configuration is incomplete')
        return
    prefix = f'users/{uid}/'
    versioned = (client.get_bucket_versioning(Bucket=bucket) or {}).get('Status') in ('Enabled', 'Suspended')
    marker = version_marker = ''
    while True:
        if versioned:
            page = client.list_objects_versions(Bucket=bucket, Prefix=prefix,
                KeyMarker=marker, VersionIdMarker=version_marker, MaxKeys=1000)
            objects = list(page.get('Version', [])) + list(page.get('DeleteMarker', []))
        else:
            page = client.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
            objects = page.get('Contents', [])
        # Validate the entire page before issuing any delete in that page.
        if any(not item['Key'].startswith(prefix) for item in objects):
            raise RuntimeError('Cloud listing escaped the account prefix')
        for item in objects:
            args = {'Bucket': bucket, 'Key': item['Key']}
            if versioned:
                args['VersionId'] = item['VersionId']
            client.delete_object(**args)
        if str(page.get('IsTruncated', 'false')).lower() != 'true':
            break
        next_marker = page.get('NextKeyMarker' if versioned else 'NextMarker')
        next_version = page.get('NextVersionIdMarker', '') if versioned else ''
        if not next_marker or (next_marker, next_version) == (marker, version_marker):
            raise RuntimeError('Cloud pagination did not advance')
        marker, version_marker = next_marker, next_version
