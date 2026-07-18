#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通过邮箱查找用户信息（含密码）
"""

import os
import sys
import sqlite3
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
USERS_DB = os.path.join(BASE_DIR, "configs", "users.db")
USERS_ROOT = os.path.join(BASE_DIR, "users")


def find_user_by_email(email: str) -> list:
    email = email.strip().lower()
    results = []

    # 1. 查 users.db（新系统，密码为哈希）
    if os.path.exists(USERS_DB):
        try:
            conn = sqlite3.connect(USERS_DB)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT id, email, password_hash, display_name, created_at FROM users WHERE email = ?", (email,))
            row = cur.fetchone()
            conn.close()
            if row:
                results.append({
                    "source": "users.db",
                    "user_id": row["id"],
                    "email": row["email"],
                    "password": row["password_hash"],
                    "password_type": "hash (werkzeug)",
                    "display_name": row["display_name"],
                    "created_at": row["created_at"]
                })
        except Exception as e:
            print(f"[WARN] users.db 查询失败: {e}")

    # 2. 查旧系统的 user_settings.json（可能存明文密码）
    if os.path.exists(USERS_ROOT):
        for uid_dir in os.listdir(USERS_ROOT):
            cfg_path = os.path.join(USERS_ROOT, uid_dir, "configs", "user_settings.json")
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    ue = (data.get("email") or "").strip().lower()
                    if ue == email:
                        pw = data.get("password", "")
                        results.append({
                            "source": "configs/users/%s/user_settings.json" % uid_dir,
                            "user_id": uid_dir,
                            "email": ue or data.get("email", ""),
                            "password": pw,
                            "password_type": "plaintext" if pw else "none",
                            "display_name": data.get("current_user_name", ""),
                            "created_at": "-"
                        })
                except Exception as e:
                    print(f"[WARN] 读取 {cfg_path} 失败: {e}")

    return results


def list_all_users() -> list:
    users = {}
    seen_emails = set()

    # users.db
    if os.path.exists(USERS_DB):
        try:
            conn = sqlite3.connect(USERS_DB)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT id, email, password_hash, display_name, created_at FROM users ORDER BY id")
            for row in cur.fetchall():
                uid = row["id"]
                em = row["email"]
                seen_emails.add(em.lower())
                users[em.lower()] = {
                    "user_id": uid,
                    "email": em,
                    "password": row["password_hash"],
                    "password_type": "hash",
                    "display_name": row["display_name"] or "",
                    "created_at": row["created_at"] or "-"
                }
            conn.close()
        except Exception as e:
            print(f"[WARN] users.db 查询失败: {e}")

    # 旧系统 user_settings.json
    if os.path.exists(USERS_ROOT):
        for uid_dir in os.listdir(USERS_ROOT):
            cfg_path = os.path.join(USERS_ROOT, uid_dir, "configs", "user_settings.json")
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    em = (data.get("email") or "").strip().lower()
                    if em and em not in seen_emails:
                        pw = data.get("password", "")
                        users[em] = {
                            "user_id": uid_dir,
                            "email": em,
                            "password": pw,
                            "password_type": "plaintext" if pw else "none",
                            "display_name": data.get("current_user_name", ""),
                            "created_at": "-"
                        }
                except Exception:
                    pass

    return list(users.values())


if __name__ == "__main__":
    if len(sys.argv) > 1:
        email = sys.argv[1]
        print(f"查询邮箱: {email}\n")
        results = find_user_by_email(email)
        if results:
            for i, r in enumerate(results):
                print(f"--- 结果 {i+1} (来源: {r['source']}) ---")
                print(f"  用户ID:     {r['user_id']}")
                print(f"  邮箱:       {r['email']}")
                print(f"  密码:       {r['password']}")
                print(f"  密码类型:   {r['password_type']}")
                print(f"  昵称:       {r['display_name']}")
                print(f"  注册时间:   {r['created_at']}")
                print()
        else:
            print(f"未找到邮箱为 '{email}' 的用户")
    else:
        print("用法: python find_user_by_email.py <邮箱地址>")
        print()
        print("示例: python find_user_by_email.py test@example.com")
