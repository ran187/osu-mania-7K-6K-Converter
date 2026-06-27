#!/usr/bin/env python3
"""
osu! Beatmap Download Converter
====================================================================

Checks the Downloads folder for .osz beatmap packages, extracts them,
converts 7K mania beatmaps to 6K using both b.py (remove long notes)
and c.py (preserve long notes), then re-packages as .osz.

Each 7K beatmap in the package gets two 6K copies:
  - _[726k].osu   — all notes become normal (b.py)
  - _[726k_ln].osu   — non-column-4 long notes preserved (c.py)
"""

import os
import sys
import zipfile
import tempfile
import shutil
import glob

# Import conversion functions from sibling scripts.
# Both modules guard main() with "if __name__ == '__main__'", so the import
# is side-effect-free apart from defining their functions.
import b
import c

# ====================== Configuration ======================

DOWNLOADS = r'C:\Users\SmdSa\Downloads'


# ====================== Core ======================

def find_osz_files(downloads_dir):
    """Return a sorted list of absolute paths to .osz files."""
    pattern = os.path.join(downloads_dir, '*.osz')
    return sorted(glob.glob(pattern))


def process_osz(osz_path):
    """
    Extract *osz_path* to a temporary directory, convert every 7K mania
    .osu file found inside (both b.py and c.py style), then re-pack
    everything back into the original .osz.

    Returns True if at least one conversion took place, False otherwise.
    """
    base = os.path.basename(osz_path)
    print(f"\n  Processing: {base}")

    tmp = tempfile.mkdtemp(prefix='osu_conv_')
    converted_any = False

    try:
        # ---- 1. Extract ----
        with zipfile.ZipFile(osz_path, 'r') as zf:
            zf.extractall(tmp)

        # ---- 2. Walk & convert ----
        for root, _dirs, files in os.walk(tmp):
            for fname in files:
                if not fname.lower().endswith('.osu'):
                    continue
                full = os.path.join(root, fname)

                if not b.is_mania_7k(full):
                    continue          # not 7K mania — skip

                r_b = b.convert_osu_file(full)
                r_c = c.convert_osu_file(full)

                if r_b:
                    print(f"    b.py  →  {os.path.basename(r_b)}")
                    converted_any = True
                if r_c:
                    print(f"    c.py  →  {os.path.basename(r_c)}")
                    converted_any = True

        if not converted_any:
            print("    (no 7K mania beatmaps found)")

        # ---- 3. Re-pack (write to temp file first for safety) ----
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


# ====================== Main ======================

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
    answer = input("Convert all? (Y/N): ").strip().upper()

    if answer != 'Y':
        print("Exiting.")
        return

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
