#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出表情文件夹里所有描述，按出现次数排序。
开心、开心（1）、开心（2） 归一为同一描述「开心」后统计。
"""
import os
import re
import sys

# 项目根目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STICKERS_ROOT = os.path.join(BASE_DIR, "stickers")
STICKER_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
COVER_BASENAME = "cover"


def normalize_description(name: str) -> str:
    """将 开心（1）、开心（2） 等归一为 开心；无括号则返回原名。"""
    if not name:
        return name
    # 去掉末尾的 （1）（2） 或 (1)(2)
    normalized = re.sub(r"[（(]\d+[）)]?$", "", name.strip()).strip()
    return normalized if normalized else name


def list_official_packs():
    if not os.path.isdir(STICKERS_ROOT):
        return []
    return [
        d for d in os.listdir(STICKERS_ROOT)
        if os.path.isdir(os.path.join(STICKERS_ROOT, d)) and not d.startswith(".")
    ]


def collect_all_names():
    """收集所有表情文件名（无扩展名），返回 [(raw_name, pack_id), ...]"""
    names = []
    for pack_id in list_official_packs():
        pack_dir = os.path.join(STICKERS_ROOT, pack_id)
        for f in os.listdir(pack_dir):
            if f.startswith("."):
                continue
            low = f.lower()
            if any(low.endswith(ext) for ext in STICKER_IMAGE_EXT):
                name_no_ext = os.path.splitext(f)[0]
                if name_no_ext.lower() == COVER_BASENAME:
                    continue
                names.append((name_no_ext, pack_id))
    return names


def main():
    names = collect_all_names()
    # 描述 -> 出现次数
    count_by_desc = {}
    # 描述 -> 原始名称列表（便于核对）
    raw_by_desc = {}

    for raw_name, pack_id in names:
        desc = normalize_description(raw_name)
        count_by_desc[desc] = count_by_desc.get(desc, 0) + 1
        if desc not in raw_by_desc:
            raw_by_desc[desc] = []
        raw_by_desc[desc].append((raw_name, pack_id))

    # 按出现次数降序
    sorted_descs = sorted(count_by_desc.items(), key=lambda x: -x[1])

    # 输出：描述 \t 次数 \t 示例原始名
    print("描述\t出现次数\t示例")
    print("-" * 60)
    for desc, count in sorted_descs:
        examples = sorted(set(r[0] for r in raw_by_desc[desc]))[:5]
        examples_str = "、".join(examples)
        if len(examples) < len(raw_by_desc[desc]):
            examples_str += " …"
        print(f"{desc}\t{count}\t{examples_str}")

    # 可选：写入文件
    out_path = os.path.join(BASE_DIR, "configs", "sticker_descriptions_sorted.txt")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("描述\t出现次数\t示例\n")
        f.write("-" * 60 + "\n")
        for desc, count in sorted_descs:
            examples = sorted(set(r[0] for r in raw_by_desc[desc]))[:5]
            examples_str = "、".join(examples)
            if len(examples) < len(raw_by_desc[desc]):
                examples_str += " …"
            f.write(f"{desc}\t{count}\t{examples_str}\n")
    print(f"\n已写入: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
