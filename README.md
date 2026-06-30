# osu!mania 7K → 6K Converter

A set of three Python scripts that convert osu!mania 7-key (7K) beatmaps to 6-key (6K) mode, with flexible long-note handling and batch processing capabilities.

---

## Features

### a.py — Download Package Converter (Preserve Long Notes)

Scans your **Downloads** folder for `.osz` beatmap packages, extracts them, runs **c.py** on every 7K beatmap inside, then re-packs everything back into `.osz`. Each 7K beatmap gets one 6K version with long notes preserved. Output files are suffixed with `_[726k_ln]`.

### b.py — Batch Converter (Remove Long Notes)

Walks your osu! **Songs** folder, finds every 7K mania `.osu` file, and creates a 6K copy. **All long notes (holds) become normal notes.** This produces a cleaner, simpler chart where every note is a single tap. Output files are suffixed with `_[726k]`.

### c.py — Batch Converter (Preserve Long Notes)

Same as b.py, but **long notes on columns 1, 2, 3, 5, 6, 7 are preserved.** Column 4 notes are always converted to normal notes. The transfer algorithm uses a unified interval model with signed distances to safely place column-4 notes without colliding with long-note bodies. Output files are suffixed with `_[726k_ln]`.

| Script | Scope | Long Notes | Output Suffix |
|--------|-------|------------|---------------|
| a.py | Downloads `.osz` | Preserved (c.py) | `_[726k_ln]` |
| b.py | Songs folder | Removed | `_[726k]` |
| c.py | Songs folder | Preserved | `_[726k_ln]` |

---

## Author

All three scripts (a.py, b.py, c.py) and the readme were written by **Claude** (Anthropic's AI assistant), with design guidance and testing from the project owner.

> 🤖 Generated with [Claude Code](https://claude.com/claude-code)

---

## Requirements

- **Python 3.7+** (standard library only — no `pip install` needed)
- Windows, macOS, or Linux
- osu! installed (for the Songs folder path)

---

## Setup — Change the Hardcoded Paths

Before running, you **must** update the folder paths inside each script to match your own machine. Open each `.py` file and look for these lines:

### In `b.py` and `c.py` (line ~589 / ~591):

```python
def get_songs_dir():
    return r'C:\Users\SmdSa\AppData\Local\osu!\Songs'
```

Change the path to your own osu! Songs folder:
- **Windows:** `C:\Users\YourName\AppData\Local\osu!\Songs`
- **macOS:** `/Users/YourName/Library/Application Support/osu!/Songs` (or wherever osu! is installed via Wine)
- **Linux (Wine):** `~/.wine/drive_c/osu!/Songs`

To find your osu! folder: open osu! → **Options** → click **"Open osu! folder"** → enter the `Songs` subdirectory.

### In `a.py` (line ~28):

```python
DOWNLOADS = r'C:\Users\SmdSa\Downloads'
```

Change this to your Downloads folder path.

---

## Usage

Open a terminal (Command Prompt, PowerShell, or bash) in the project directory and run:

### a.py — Convert downloaded .osz packages

```bash
python a.py
```

1. Lists all `.osz` files in your Downloads folder.
2. Asks `Convert all? (Y/N):` — type **Y** to proceed, **N** to exit.
3. For each `.osz`: extracts → converts all 7K beatmaps (preserving LN) → re-packs.
4. The converted `.osz` can be opened directly in osu! (Ctrl+O or double-click).

### b.py — Batch-convert entire Songs folder (remove LN)

```bash
python b.py
```

1. Scans every subfolder under your osu! Songs directory.
2. The script prints a prompt with the beatmap count and waits for **Enter** before starting.
3. Original files are **never modified**.
4. For each 7K mania `.osu` file, creates a `_[726k].osu` copy alongside the original.

### c.py — Batch-convert entire Songs folder (preserve LN)

```bash
python c.py
```

1. Same as b.py, but output files use the `_[726k_ln].osu` suffix and preserve long notes.

---

## How the Conversion Works

The 7K → 6K conversion removes the **4th column** (0-indexed: column 3). Notes on that column are handled as follows:

- **Alone at their timestamp** → transferred to the best-fitting 6K column.
- **With company** (other columns playing at the same moment) → discarded.

Columns 1, 2, 3 become columns 1, 2, 3; columns 5, 6, 7 become columns 4, 5, 6.

Metadata is updated: `BeatmapID` set to 0, `CircleSize` set to 6, `Creator` and `Version` tagged to indicate the conversion.

---

## Notes

- **a.py imports c.py** — keep both files in the same folder. b.py is standalone.
- The conversion is **non-destructive**: original `.osu` files are never overwritten. b.py and c.py create new files alongside the originals; a.py re-packs the `.osz` with both old and new files.
- Tested on ~10 beatmaps; no issues found. If you encounter problems, please report them.
