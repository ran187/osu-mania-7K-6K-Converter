#!/usr/bin/env python3
"""
osu! 谱面下载包转换器（密度感知的第 4 列转移）
==============================================

扫描 Downloads 文件夹中的 .osz 谱面包：解压 → 用 b.py 把其中的
7K mania 谱面转换为 6K（密度感知的第 4 列转移）→ 重新打包为 .osz。

谱面包中的每个 7K 谱面都会得到一份 6K 副本：
  - _[726k].osu   — 第 4 列音符按密度检查转移或丢弃（b.py）
"""

import os
import sys
import zipfile
import tempfile
import shutil
import glob

# 从 b.py 导入转换函数
import b

# ====================== 配置 ======================

DOWNLOADS = r'C:\Users\SmdSa\Downloads'


# ====================== 核心 ======================

def find_osz_files(downloads_dir):
    """返回 downloads_dir 下所有 .osz 文件的绝对路径（有序）。"""
    pattern = os.path.join(downloads_dir, '*.osz')
    return sorted(glob.glob(pattern))


def process_osz(osz_path):
    """
    把 *osz_path* 解压到临时目录，转换其中所有 7K mania 谱面
    （b.py 风格，密度感知的第 4 列转移），再整体打包回原 .osz。

    至少转换了一个谱面时返回 True，否则返回 False。
    """
    base = os.path.basename(osz_path)
    print(f"\n  Processing: {base}")

    tmp = tempfile.mkdtemp(prefix='osu_conv_')
    converted_any = False

    try:
        # ---- 1. 解压 ----
        with zipfile.ZipFile(osz_path, 'r') as zf:
            zf.extractall(tmp)

        # ---- 2. 遍历并转换 ----
        for root, _dirs, files in os.walk(tmp):
            for fname in files:
                if not fname.lower().endswith('.osu'):
                    continue
                full = os.path.join(root, fname)

                if not b.is_mania_7k(full):
                    continue          # 非 7K mania —— 跳过

                r_b = b.convert_osu_file(full)

                if r_b:
                    print(f"    b.py  →  {os.path.basename(r_b)}")
                    converted_any = True

        if not converted_any:
            print("    (no 7K mania beatmaps found)")

        # ---- 3. 重新打包（先写临时文件再原子替换，避免半成品） ----
        if converted_any:
            tmp_osz = osz_path + '.tmp'
            with zipfile.ZipFile(tmp_osz, 'w', zipfile.ZIP_DEFLATED) as zf:
                for root, _dirs, files in os.walk(tmp):
                    for fname in files:
                        file_path = os.path.join(root, fname)
                        arcname = os.path.relpath(file_path, tmp)
                        zf.write(file_path, arcname)
            os.replace(tmp_osz, osz_path)
            print(f"    Repacked: {base}")

        return converted_any

    except Exception as e:
        print(f"    ERROR: {e}")
        return False

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ====================== 主程序 ======================

def main():
    print("=" * 50)
    print("  osu! Beatmap Download Converter")
    print("=" * 50)
    print()

    downloads = DOWNLOADS
    if not os.path.isdir(downloads):
        print(f"Downloads folder not found: {downloads}")
        sys.exit(1)

    osz_files = find_osz_files(downloads)

    if not osz_files:
        print("No .osz files found in Downloads.")
        return

    print(f"Found {len(osz_files)} .osz file(s) in Downloads:\n")
    for f in osz_files:
        print(f"  {os.path.basename(f)}")

    print()
    input("Press Enter to start conversion...")
    print("\n" + "=" * 45)

    converted = 0
    skipped = 0

    for osz_path in osz_files:
        try:
            if process_osz(osz_path):
                converted += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"    UNEXPECTED ERROR: {e}")
            skipped += 1

    print()
    print("=" * 45)
    print(f"  Converted : {converted}")
    print(f"  Skipped   : {skipped}")
    print("=" * 45)


if __name__ == '__main__':
    main()
