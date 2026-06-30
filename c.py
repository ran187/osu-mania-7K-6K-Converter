#!/usr/bin/env python3
"""
osu!mania 7K -> 6K Beatmap Converter (Preserve Long Notes)
====================================================================

Converts osu!mania 7-key (7K) beatmaps to 6-key (6K) mode.

Differences from b.py:
  1. Non-column-4 long notes (holds) are PRESERVED as long notes.
     b.py converts ALL long notes to normal notes; this script keeps
     columns 1,2,3,5,6,7 long notes intact.
  2. Column-4 notes are all treated as normal notes (same as b.py).
  3. Unified interval-based transfer: every note on a 6K column creates
     an interval — [T-250, T+250] for normal notes, [S-250, E+250] for
     long notes.  A transfer candidate must fall OUTSIDE all intervals
     on a column for it to be eligible.  The column with the largest
     minimum distance to the nearest interval edge is chosen.
     (This approach also applies to b.py and yields the same result.)

Conversion rules:
  1. Metadata: append "& ssaj" to Creator, append "_726k_ln" to Version,
     set BeatmapID to 0, set CircleSize to 6.
  2. [HitObjects]:
     - Columns 1,2,3,5,6,7 (0-indexed: 0,1,2,4,5,6):
         Remap to 6K columns.  Long notes STAY long.
     - Column 4 (0-indexed: 3):
         * If alone at its timestamp → transfer to the best 6K column.
           Signed distance to nearest interval edge is computed per column:
             - T outside interval → positive (distance to nearer edge)
             - T inside  interval → negative (−distance to nearer edge)
             - Empty column       → +∞
           Two aggregates across ALL 6 columns:
             最小最小值 a = MIN(signed distances): if a < 0, discard
             (T inside some column's interval → horizontal crowding).
             最大最小值 b = MAX(signed distances): pick column with b
             (widest gap → best vertical spacing; b > 0 when a ≥ 0).
         * If company present → discard.
         * Always becomes a normal note (type 1).
     - Columns are remapped: col<3 stay, col>3 shift down by 1.
  3. A new .osu file is created with "_[726k_ln]" suffix; originals untouched.
"""

import bisect
import os
import random
import re

# ====================== Constants ======================

ORIGINAL_KEYS = 7          # 7K input
TARGET_KEYS = 6            # 6K output
DELETED_COL = 3            # 0-indexed column that gets removed (the "4th" column)
TYPE_NORMAL = 1            # Hit object type for a normal (non-hold) note
TYPE_HOLD = 128            # Bit mask for mania long-note / hold
NEW_COMBO_FLAG = 4         # New-combo bit (osu! type field bit 2)

# Unified interval margin (ms).
# Every note on a 6K column is modelled as an interval:
#   - Normal note at time T  →  [T - MARGIN,  T + MARGIN]   (500 ms wide)
#   - Long note from S to E  →  [S - MARGIN,  E + MARGIN]
# A transfer candidate must fall strictly outside all intervals on a
# column; the edge-distance from the candidate to the nearest interval
# boundary is then used to rank eligible columns.
INTERVAL_MARGIN = 250


# ====================== Helper Functions ======================

def get_column(x, key_count):
    """
    Return the 0-indexed column number from an x coordinate.

    Formula from the osu! spec:  column = floor(x * keyCount / 512)
    """
    return int(x * key_count / 512)


def get_new_x(col, key_count=TARGET_KEYS):
    """
    Return the x coordinate for the CENTRE of a column in the target key count.

    x = floor((col + 0.5) * 512 / key_count)
    """
    return int((col + 0.5) * 512 / key_count)


# ====================== Hit Object Parsing ======================

def parse_hit_object(line):
    """
    Parse a single [HitObjects] line.

    Two formats are handled:
      - Normal : x, y, time, type, hitSound, hitSample
      - Hold   : x, y, time, type, hitSound, endTime:hitSample

    Returns a dict, or None if the line is invalid.
    """
    line = line.strip()
    if not line:
        return None

    parts = line.split(',')
    if len(parts) < 5:      # at least x,y,time,type,hitsound
        return None

    try:
        x = int(parts[0])
        y = int(parts[1])
        time = int(parts[2])
        obj_type = int(parts[3])
        hit_sound = int(parts[4])
    except ValueError:
        return None

    is_long = bool(obj_type & TYPE_HOLD)        # 128 -> true
    is_new_combo = bool(obj_type & NEW_COMBO_FLAG)  # 4 -> true
    end_time = None
    hit_sample = ''

    # Everything after field 5 depends on whether this is a hold note.
    remainder = ','.join(parts[5:])     # usually remainder == parts[5] without ,

    if is_long:
        # Format: endTime:hitSample
        colon_idx = remainder.find(':')
        if colon_idx != -1:
            try:
                end_time = int(remainder[:colon_idx])
            except ValueError:
                end_time = 0
            hit_sample = remainder[colon_idx + 1:]
        else:
            # Degenerate case — treat entire remainder as endTime
            try:
                end_time = int(remainder)
            except ValueError:
                end_time = 0
            hit_sample = ''
    else:
        # Normal note — the remainder IS the hitSample
        hit_sample = remainder

    return {
        'x': x,
        'y': y,
        'time': time,
        'type': obj_type,
        'hitSound': hit_sound,
        'endTime': end_time,
        'hitSample': hit_sample,
        'is_long': is_long,
        'is_new_combo': is_new_combo,
    }


def format_hit_object(obj, ensure_new_combo=False):
    """
    Serialise a hit-object dict back to a .osu line.

    - Normal notes:  type 1, or 5 when it is the very first note in the
                     beatmap (ensure_new_combo=True) or originally had
                     the new-combo flag (is_new_combo=True).
    - Long notes:    type is ALWAYS 128 — the new-combo flag is never
                     applied to long notes, even when they are the first
                     note in the beatmap.
                     The tail uses "endTime:hitSample" format, matching
                     the convention seen in mania beatmaps.
    """
    if obj.get('is_long'):
        # Long note: type is unconditionally HOLD (128)
        t = TYPE_HOLD
    else:
        # Normal note: type = 1, or 5 if first note / originally had new-combo
        is_new_combo = ensure_new_combo or obj.get('is_new_combo')
        t = TYPE_NORMAL | NEW_COMBO_FLAG if is_new_combo else TYPE_NORMAL

    if obj.get('is_long') and obj.get('endTime') is not None:
        # Long note format: endTime:hitSample
        return (f"{obj['x']},{obj['y']},{obj['time']},"
                f"{t},{obj['hitSound']},{obj['endTime']}:{obj['hitSample']}")
    else:
        # Normal note format
        return (f"{obj['x']},{obj['y']},{obj['time']},"
                f"{t},{obj['hitSound']},{obj['hitSample']}")


# ====================== Grouping ======================

def read_groups(hit_object_lines):
    """
    Yield groups of hit objects that share the same start time.

    Each element:  (time_in_ms, [list_of_parsed_obj_dicts])

    Because [HitObjects] are sorted by time we can simply compare
    consecutive lines.
    """
    current_time = None
    current_group = []

    for line in hit_object_lines:
        obj = parse_hit_object(line)
        if obj is None:
            continue

        if current_time is None:
            current_time = obj['time']
            current_group = [obj]
        elif obj['time'] == current_time:
            current_group.append(obj)
        else:
            yield (current_time, current_group)
            current_time = obj['time']
            current_group = [obj]

    if current_group:
        yield (current_time, current_group)


# ====================== Transfer Resolution ======================

def resolve_transfers(transfer_candidates, col_intervals):
    """
    Resolve column-4-alone notes that must be transferred to another column.

    Unified interval model
    ----------------------
    Every non-column-4 note is represented as an interval:
      - Normal note at time T   →  [T - MARGIN,  T + MARGIN]
      - Long note from S to E   →  [S - MARGIN,  E + MARGIN]

    For each transfer candidate at time T, we compute a *signed* distance
    from T to the nearest interval edge on each of the 6 target columns:
      - T is LEFT  of the interval  →  signed_dist = start - T   (> 0)
      - T is RIGHT of the interval  →  signed_dist = T - end     (> 0)
      - T is INSIDE the interval    →  signed_dist = -(distance to nearer edge)  (< 0)
      - Column has no intervals     →  signed_dist = +∞

    Two aggregate metrics across ALL 6 columns:
      a. 最小最小值 a = MIN(signed_dist across all 6 columns).
         If a < 0, T falls inside at least one column's interval →
         the note would cause horizontal crowding; DISCARD it.
      b. 最大最小值 b = MAX(signed_dist across all 6 columns).
         When a ≥ 0, T is outside all intervals on all columns, so
         b > 0 is guaranteed.  The column with signed_dist == b is the
         one with the widest gap — best vertical spacing (纵向上).
         When multiple columns tie at b, pick randomly among them.

    Resolved notes are always normal notes (type 1), not long notes.

    Parameters
    ----------
    transfer_candidates : list[dict]
        Parsed hit-object dicts needing transfer, already in time order.
    col_intervals : list[list[tuple[int, int]]]
        Six lists of (start, end) intervals, one per 6K column,
        each sorted by start time.

    Returns
    -------
    list[dict]
        New note dicts (with correct x for their assigned column).
    """
    resolved = []

    for obj in transfer_candidates:
        T = obj['time']
        best_cols = []
        best_min_dist = -1                     # 最大最小值 b (best signed distance)
        global_min_dist = float('inf')         # 最小最小值 a (worst signed distance)

        for col in range(TARGET_KEYS):
            intervals = col_intervals[col]

            if not intervals:
                # Column is completely empty — ideal choice
                signed_dist = float('inf')
            else:
                signed_dist = float('inf')

                for start, end in intervals:
                    if start <= T <= end:
                        # T is INSIDE this interval → signed distance is NEGATIVE
                        # Use the negated distance to the nearer edge
                        inside_dist = -(min(T - start, end - T))
                        signed_dist = min(signed_dist, inside_dist)
                    elif T < start:
                        # T is left of this interval
                        signed_dist = min(signed_dist, start - T)
                    else:  # T > end
                        # T is right of this interval
                        signed_dist = min(signed_dist, T - end)

            # ---- Track 最小最小值 a (minimum signed distance) ----
            if signed_dist < global_min_dist:
                global_min_dist = signed_dist

            # ---- Track 最大最小值 b (maximum signed distance) ----
            if signed_dist > best_min_dist:
                best_min_dist = signed_dist
                best_cols = [col]
            elif signed_dist == best_min_dist:
                best_cols.append(col)

        # ---- 最小最小值 a < 0 → T falls inside some column's interval → discard ----
        if global_min_dist < 0:
            continue

        # ---- Best column (b > 0 guaranteed since a ≥ 0) ----
        target_col = random.choice(best_cols)
        new_x = get_new_x(target_col)

        new_obj = {
            'x': new_x,
            'y': obj['y'],
            'time': obj['time'],
            'hitSound': obj['hitSound'],
            'hitSample': obj['hitSample'],
            'is_new_combo': obj['is_new_combo'],
            'is_long': False,           # column-4 notes always become normal
            'endTime': None,
        }
        resolved.append(new_obj)

    return resolved


# ====================== Conversion Core ======================

def convert_hit_objects(hit_object_lines):
    """
    Convert the [HitObjects] block from 7K to 6K.

    Three-phase algorithm:

      Phase 1 — Single pass over groups (time order):
        - Non-column-4 notes: remap column & x, PRESERVE long notes,
          add to the output pool.  Build a unified interval list per
          column (sorted by start time) for later transfer resolution.
        - Column-4-alone notes:  pushed to a transfer-candidate list.
        - Column-4-with-company:  silently discarded.

      Phase 2 — Resolve transfers (time order, using resolve_transfers):
        - For each deferred note, check all 6 columns' intervals.
          A column is eligible only if the candidate falls outside
          every interval on that column.  Among eligible columns, pick
          the one with the largest minimum distance to the nearest
          interval edge.

      Phase 3 — Merge regular + resolved notes, sort by (time, x),
                serialise with format_hit_object.
    """
    # Per-column unified intervals — sorted by start time
    # Each interval is (start, end) where:
    #   normal note at T → (T - MARGIN, T + MARGIN)
    #   long note S..E   → (S - MARGIN, E + MARGIN)
    col_intervals = [[] for _ in range(TARGET_KEYS)]

    regular_notes = []          # output-ready dicts (remapped / converted)
    transfer_candidates = []    # column-4-alone notes, already in time order

    # ---- Phase 1 --------------------------------------------------------
    for _time, group in read_groups(hit_object_lines):

        # Annotate each object with its 7K column
        cols_7k = set()
        for obj in group:
            c = get_column(obj['x'], ORIGINAL_KEYS)
            obj['_col_7k'] = c
            cols_7k.add(c)

        other_cols = cols_7k - {DELETED_COL}
        is_col3_alone = (DELETED_COL in cols_7k) and (not other_cols)

        for obj in group:
            col = obj['_col_7k']

            if col == DELETED_COL:
                if is_col3_alone:
                    transfer_candidates.append(obj)
                # else: col 3 has company → discard
            else:
                # Remap column
                new_col = col - 1 if col > DELETED_COL else col
                new_x = get_new_x(new_col)

                new_obj = {
                    'x': new_x,
                    'y': obj['y'],
                    'time': obj['time'],
                    'hitSound': obj['hitSound'],
                    'hitSample': obj['hitSample'],
                    'is_new_combo': obj['is_new_combo'],
                    'is_long': obj['is_long'],          # preserve long notes
                    'endTime': obj['endTime'],          # preserve end time
                }
                regular_notes.append(new_obj)

                # ---- Build unified interval ----
                if obj['is_long'] and obj['endTime'] is not None:
                    iv_start = obj['time'] - INTERVAL_MARGIN
                    iv_end = obj['endTime'] + INTERVAL_MARGIN
                else:
                    iv_start = obj['time'] - INTERVAL_MARGIN
                    iv_end = obj['time'] + INTERVAL_MARGIN

                # Insert sorted by start time
                ins_idx = bisect.bisect_left(
                    col_intervals[new_col], (iv_start, iv_end))
                col_intervals[new_col].insert(ins_idx, (iv_start, iv_end))

    # ---- Phase 2 --------------------------------------------------------
    resolved_notes = resolve_transfers(transfer_candidates, col_intervals)

    # ---- Phase 3 --------------------------------------------------------
    all_notes = regular_notes + resolved_notes
    all_notes.sort(key=lambda o: (o['time'], o['x']))

    output = []
    is_first = True
    for obj in all_notes:
        output.append(format_hit_object(obj, ensure_new_combo=is_first))
        is_first = False

    return output


# ====================== File-Level Conversion ======================

def _modify_creator(line):
    """Append '& ssaj' to the Creator metadata value."""
    m = re.match(r'(Creator\s*:\s*)(.*)', line)
    if m:
        return f"{m.group(1)}{m.group(2).rstrip()}& ssaj\n"
    return line


def _modify_version(line):
    """Append '_726k_ln' to the Version metadata value."""
    m = re.match(r'(Version\s*:\s*)(.*)', line)
    if m:
        return f"{m.group(1)}{m.group(2).rstrip()}_726k_ln\n"
    return line


def _modify_beatmap_id(line):
    """Force BeatmapID to 0."""
    return re.sub(r'(BeatmapID\s*:\s*)\d+', r'\g<1>0', line)


def _modify_circle_size(line):
    """Force CircleSize to 6."""
    return re.sub(r'(CircleSize\s*:\s*)\d+', r'\g<1>6', line)


def is_mania_7k(osu_path):
    """
    Return True if *osu_path* is a mania-7K beatmap.

    Reads the file line-by-line and stops early — Mode and CircleSize
    always appear in the first ~40 lines ([General] and [Difficulty]
    sections)

    Checks:
      - Mode: 3          (osu!mania)
      - CircleSize: 7    (7 keys)
    """
    mode = None
    circle_size = None

    try:
        with open(osu_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                stripped = line.strip()

                # ---- Section headers ----
                # Once we pass [HitObjects] we can stop
                if stripped == '[HitObjects]':
                    break

                # ---- Mode (in [General]) ----
                if mode is None and stripped.startswith('Mode:'):
                    try:
                        mode = int(stripped.split(':')[1].strip())
                    except ValueError:
                        pass
                    if mode != 3:
                        return False

                # ---- CircleSize (in [Difficulty]) ----
                if circle_size is None and stripped.startswith('CircleSize:'):
                    try:
                        circle_size = int(stripped.split(':')[1].strip())
                    except ValueError:
                        pass
                    if circle_size != 7:
                        return False

                # Both checks done — no need to read further
                if mode is not None and circle_size is not None:
                    break

    except Exception:
        return False

    return mode == 3 and circle_size == 7


def convert_osu_file(osu_path):
    """
    Convert one .osu file from 7K to 6K.

    Returns the path of the newly-created file, or None on failure.
    The original file is never modified.
    """
    # ---- Read original ----
    try:
        with open(osu_path, 'r', encoding='utf-8-sig') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"    ERROR reading file: {e}")
        return None

    # ---- Locate [HitObjects] ----
    hit_objects_header_idx = None
    raw_hit_object_lines = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '[HitObjects]':
            hit_objects_header_idx = i
            continue
        if hit_objects_header_idx is not None:
            if stripped.startswith('['):
                break          # next section — shouldn't happen, but be safe
            if stripped:
                raw_hit_object_lines.append(stripped)

    if hit_objects_header_idx is None:
        print(f"    No [HitObjects] section — skipping")
        return None
    if not raw_hit_object_lines:
        print(f"    [HitObjects] is empty — skipping")
        return None

    # ---- Convert HitObjects ----
    converted_hos = convert_hit_objects(raw_hit_object_lines)

    # ---- Assemble output ----
    output_lines = []

    for line in lines:
        stripped = line.strip()

        if stripped == '[HitObjects]':
            # [HitObjects] is always the last section per the osu! spec,
            # so we can write the converted objects and stop immediately.
            output_lines.append(line)
            for ho in converted_hos:
                output_lines.append(ho + '\n')
            output_lines.append('\n')
            break

        # ---- Apply metadata patches before [HitObjects] ----
        if stripped.startswith('Creator:'):
            line = _modify_creator(line)
        elif stripped.startswith('Version:'):
            line = _modify_version(line)
        elif stripped.startswith('BeatmapID:'):
            line = _modify_beatmap_id(line)
        elif stripped.startswith('CircleSize:'):
            line = _modify_circle_size(line)

        output_lines.append(line)

    # ---- Write new file ----
    dir_name = os.path.dirname(osu_path)
    base_name = os.path.basename(osu_path)
    stem, ext = os.path.splitext(base_name)
    new_filename = f"{stem}_[726k_ln]{ext}"
    new_path = os.path.join(dir_name, new_filename)

    try:
        with open(new_path, 'w', encoding='utf-8', newline='') as f:
            f.writelines(output_lines)
        return new_path
    except Exception as e:
        print(f"    ERROR writing file: {e}")
        return None


# ====================== Batch Mode ======================

def get_songs_dir():
    """
    Return the osu! Songs directory.

    Hard-coded for the current user.  If you are running this script on a
    different machine, change the path below to your own osu! Songs folder.
    You can find it by opening osu! → Options → "Open osu! folder",
    then entering the "Songs" sub-directory.
    """
    return r'C:\Users\SmdSa\AppData\Local\osu!\Songs'


def batch_convert():
    """
    Walk the osu! Songs folder, find 7K mania .osu files,
    and create a converted 6K copy alongside each.
    """
    songs_dir = get_songs_dir()

    if not songs_dir:
        print("Could not auto-detect the osu! Songs folder.")
        print("Please paste the full path to your Songs folder:")
        songs_dir = input().strip().strip('"')
        if not os.path.isdir(songs_dir):
            print(f"'{songs_dir}' is not a valid directory.  Exiting.")
            return

    print(f"\nSongs folder: {songs_dir}\n")

    # Collect sub-folders (each = one beatmap set)
    try:
        entries = sorted(
            [e for e in os.listdir(songs_dir)
             if os.path.isdir(os.path.join(songs_dir, e))]
        )
    except Exception as e:
        print(f"Error listing Songs folder: {e}")
        return

    total = len(entries)
    if total == 0:
        print("No sub-folders found — nothing to do.")
        return

    print(f"Found {total} beatmap set(s).  Press Enter to start conversion...")
    input()

    converted_sets = 0
    skipped_sets = 0

    for idx, entry in enumerate(entries, start=1):
        subdir = os.path.join(songs_dir, entry)
        # Progress:  completed / total
        print(f"[{idx}/{total}]  {entry}")

        # Gather .osu files
        try:
            osu_files = [
                os.path.join(subdir, f)
                for f in os.listdir(subdir)
                if f.lower().endswith('.osu')
            ]
        except Exception:
            print("    (cannot read folder — skipping)")
            skipped_sets += 1
            continue

        any_converted = False
        for osu_path in osu_files:
            if not is_mania_7k(osu_path):
                continue          # not a 7K mania — skip silently

            result = convert_osu_file(osu_path)
            if result:
                any_converted = True

        if any_converted:
            converted_sets += 1
        else:
            skipped_sets += 1

    print()
    print("=" * 45)
    print("  Conversion complete!")
    print(f"  Beatmap sets with conversions : {converted_sets}")
    print(f"  Sets skipped (no 7K mania)    : {skipped_sets}")
    print("=" * 45)


# ====================== Main ======================

def main():
    print("=" * 50)
    print("   osu!mania  7K  -->  6K  Beatmap Converter")
    print("   (Preserve Long Notes)")
    print("=" * 50)

    batch_convert()


if __name__ == '__main__':
    main()
