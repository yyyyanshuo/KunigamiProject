# -*- coding: utf-8 -*-
import os
from qcloud_cos import CosConfig
from qcloud_cos import CosS3Client
from dotenv import load_dotenv

import mimetypes

load_dotenv()

# 1. 初始化配置
secret_id = os.getenv('COS_SECRET_ID')
secret_key = os.getenv('COS_SECRET_KEY')
region = os.getenv('COS_REGION')
bucket = os.getenv('COS_BUCKET')

config = CosConfig(Region=region, SecretId=secret_id, SecretKey=secret_key)
client = CosS3Client(config)

def upload_to_cos(local_path, cos_path):
    """
    将本地文件上传到 COS
    local_path: 本地文件路径，例如 'static/uploads/abc.jpg'
    cos_path: 存储在COS中的路径，例如 'chat_images/abc.jpg'
    """
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

def get_cos_list(prefix, get_folders=False):
    """
    prefix: 路径前缀，例如 'stickers/'
    get_folders: True则列出子文件夹名，False则列出文件名
    """
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