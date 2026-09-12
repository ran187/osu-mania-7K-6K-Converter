#!/usr/bin/env python3
"""
osu!mania 7K -> 6K Beatmap Converter (Density-Aware Column-4 Transfer)
====================================================================

Converts osu!mania 7-key (7K) beatmaps to 6-key (6K) mode.

Columns 1,2,3,5,6,7 are kept intact and simply remapped (long notes stay
long); only the 4th column (0-indexed: 3) is ever removed or moved.  A
column-4 note is discarded outright when another note shares its
timestamp; otherwise its fate is decided by a density check:

  For each column-4-alone note, count how many other notes start within
  ±250 ms (a 0.5 s window) of it:
    * count <= a  (a = NEARBY_NOTE_LIMIT, default 4)
          → transfer.  The neighbourhood is sparse, so moving the note
            keeps the original rhythm without disturbing the other
            columns.
            Example:  notes on columns 3/4/5 at 100/200/300 ms — the
            middle note has only 2 neighbours within the window, so it
            survives and lands on an idle column (1/2/6/7), keeping the
            alternating-finger run intact.
    * count > a
          → discard.  The neighbourhood is dense, so dropping the note
            protects the hand-feel of the other columns.

With the defaults (a = 4, ±250 ms) a note is dropped once 5 or more
other notes fall inside its 0.5 s window — i.e. from roughly 10 notes
per second upwards the chart is considered dense.

Conversion rules:
  1. Metadata: append "&ssaj" to Creator, append "_726k" to Version,
     set BeatmapID to 0, set CircleSize to 6.
  2. [HitObjects]:
     - Columns 1,2,3,5,6,7 (0-indexed: 0,1,2,4,5,6):
         Remap to 6K columns.  Long notes STAY long.
     - Column 4 (0-indexed: 3):
         * If company is present at its timestamp → discard.
         * Otherwise apply the density check described above; surviving
           notes are transferred to the best 6K column, provided its
           nearest interval edge is at least 62 ms away (see
           resolve_transfers).
         * Always becomes a normal note (type 1).
     - Columns are remapped: col<3 stay, col>3 shift down by 1.
  3. A new .osu file is created with a "_[726k]" suffix; originals
     untouched.
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

# Half-width of the interval around every note (ms).
# Every note on a 6K column is modelled as an interval:
#   - Normal note at time T  →  [T - MARGIN,  T + MARGIN]
#   - Long note from S to E  →  [S - MARGIN,  E + MARGIN]
# Used both by the density check for column-4 candidates (can_transfer)
# and by the interval model that ranks target columns (resolve_transfers).
INTERVAL_MARGIN = 250

# Density threshold for the column-4 transfer decision (parameter "a").
# A column-4-alone note is discarded only when MORE than this many other
# notes start within ±INTERVAL_MARGIN of it:
#   - sparse neighbourhood (count <= a) → transfer, preserving rhythm
#   - dense neighbourhood (count >  a) → discard, protecting the feel of
#     the other columns
# With a = 4 and the ±250 ms window (0.5 s total), a note is dropped
# once 5 or more other notes fall inside the window — a density of
# roughly 10 notes per second or higher.
NEARBY_NOTE_LIMIT = 4

# Minimum acceptable gap (ms) between a transferred note and the nearest
# interval edge on its target column.  After ranking the 6 target
# columns, the best signed distance (best_min_dist) must be at least
# this large; otherwise every column is too close to an existing note
# and the candidate is discarded instead of transferred.
MIN_TRANSFER_GAP = 125


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

def can_transfer(t, all_start_times, limit=NEARBY_NOTE_LIMIT):
    """
    Density check: can a column-4-alone note at time `t` be transferred?

    Counts every OTHER note whose start time falls within
    ±INTERVAL_MARGIN (250 ms, i.e. a 0.5 s window) of `t` and compares
    that count against the threshold `a` (limit):

      - count <= a  →  True   (neighbourhood sparse → transfer, keep rhythm)
      - count >  a  →  False  (neighbourhood dense  → discard, keep hand-feel)

    `all_start_times` is a sorted list of every note's start time (all
    columns, including column 4), so the count is obtained with two
    binary searches in O(log N) time.

    The candidate is alone at its own timestamp (only column-4-alone
    notes are ever checked), so exactly one entry in all_start_times
    belongs to the candidate itself and is subtracted from the count.

    With the default limit = 4 the note is dropped only when 5 or more
    other notes crowd its 0.5 s window — roughly 10 notes per second or
    denser.

    Parameters
    ----------
    t : int
        Start time of the transfer candidate.
    all_start_times : list[int]
        Sorted list of every note's start time (all columns, including col 4).
    limit : int
        The density threshold `a` — maximum tolerated number of nearby
        notes.  Defaults to NEARBY_NOTE_LIMIT.

    Returns
    -------
    bool
        True if the note can be transferred.
    """
    lo = bisect.bisect_left(all_start_times, t - INTERVAL_MARGIN)
    hi = bisect.bisect_right(all_start_times, t + INTERVAL_MARGIN)
    nearby = (hi - lo) - 1          # entries within ±INTERVAL_MARGIN, minus the candidate itself
    return nearby <= limit


def resolve_transfers(transfer_candidates, col_intervals):
    """
    Choose a target 6K column for each column-4 transfer candidate.

    The density eligibility is decided *before* calling this function
    (see can_transfer).  This function picks the destination column and
    applies one final check: the best signed distance found across the 6
    columns (best_min_dist) must be at least MIN_TRANSFER_GAP (62 ms).
    Candidates whose best gap is smaller than that would land too close
    to existing notes on every column and are discarded here.

    Unified interval model
    ----------------------
    Every non-column-4 note is represented as an interval:
      - Normal note at time T   →  [T - MARGIN,  T + MARGIN]
      - Long note from S to E   →  [S - MARGIN,  E + MARGIN]

    For each candidate at time T, a *signed* distance from T to the
    nearest interval edge is computed on each of the 6 target columns:
      - T is LEFT  of the interval  →  signed_dist = start - T   (> 0)
      - T is RIGHT of the interval  →  signed_dist = T - end     (> 0)
      - T is INSIDE the interval    →  signed_dist = -(distance to nearer edge)  (< 0)
      - Column has no intervals     →  signed_dist = +∞

    The column with the largest signed distance is chosen — it has the
    widest gap to the existing notes, so the transferred note disturbs
    the other columns as little as possible.  Empty columns (+∞) always
    win, which is how a sparse section lands on an idle column (e.g. the
    100/200/300 ms run goes to column 1/2/6/7).  When several columns
    tie, one is picked at random.  If the largest signed distance is
    below MIN_TRANSFER_GAP (62 ms), every column is too crowded and the
    candidate is discarded.

    Resolved notes are always normal notes (type 1), not long notes.

    Parameters
    ----------
    transfer_candidates : list[dict]
        Parsed hit-object dicts needing transfer, already in time order
        AND pre-filtered (all candidates are known to be transferable).
    col_intervals : list[list[tuple[int, int]]]
        Six lists of (start, end) intervals, one per 6K column,
        each sorted by start time.

    Returns
    -------
    list[dict]
        New note dicts (with correct x for their assigned column).
        Candidates rejected by the minimum-gap check are omitted.
    """
    resolved = []

    for obj in transfer_candidates:
        T = obj['time']
        best_cols = []
        best_min_dist = -float('inf')           # best signed distance seen so far

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
                        inside_dist = -(min(T - start, end - T))
                        signed_dist = min(signed_dist, inside_dist)
                    elif T < start:
                        # T is left of this interval
                        signed_dist = min(signed_dist, start - T)
                    else:  # T > end
                        # T is right of this interval
                        signed_dist = min(signed_dist, T - end)

            # ---- Track the maximum signed distance across columns ----
            if signed_dist > best_min_dist:
                best_min_dist = signed_dist
                best_cols = [col]
            elif signed_dist == best_min_dist:
                best_cols.append(col)

        # ---- Minimum-gap check: if even the best column is closer than
        #      MIN_TRANSFER_GAP to an existing note, drop the candidate ----
        if best_min_dist < MIN_TRANSFER_GAP:
            continue

        # ---- Best column ----
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
        - A global sorted list of all note start times is collected for
          the density-based transfer-eligibility check.

      Phase 2 — Density filter & resolve transfers (time order):
        - Filter transfer candidates with can_transfer: a note at time t
          survives only when at most `a` other notes start within ±250 ms
          of t (a 0.5 s window, a = NEARBY_NOTE_LIMIT).
          This uses the global start-time list (binary search).
        - Surviving candidates then go through resolve_transfers to pick
          the best target column (widest gap to the nearest interval
          edge).

      Phase 3 — Merge regular + resolved notes, sort by (time, x),
                serialise with format_hit_object.
    """
    # Per-column unified intervals — sorted by start time
    # Each interval is (start, end) where:
    #   normal note at T → (T - MARGIN, T + MARGIN)
    #   long note S..E   → (S - MARGIN, E + MARGIN)
    col_intervals = [[] for _ in range(TARGET_KEYS)]

    # Global sorted list of every note's start time (all columns, incl. col 4).
    # Used by can_transfer for the neighbourhood density check.
    all_start_times = []

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

            # ---- Record global start time (all notes, all columns) ----
            bisect.insort(all_start_times, obj['time'])

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
    # Density filter: a candidate survives only when at most `a` other
    # notes start within ±250 ms of it (a 0.5 s window, a = NEARBY_NOTE_LIMIT).
    eligible_candidates = [
        obj for obj in transfer_candidates
        if can_transfer(obj['time'], all_start_times)
    ]
    resolved_notes = resolve_transfers(eligible_candidates, col_intervals)

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
    """Append '&ssaj' to the Creator metadata value."""
    m = re.match(r'(Creator\s*:\s*)(.*)', line)
    if m:
        return f"{m.group(1)}{m.group(2).rstrip()}&ssaj\n"
    return line


def _modify_version(line):
    """Append '_726k' to the Version metadata value."""
    m = re.match(r'(Version\s*:\s*)(.*)', line)
    if m:
        return f"{m.group(1)}{m.group(2).rstrip()}_726k\n"
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
    new_filename = f"{stem}_[726k]{ext}"
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
    print(f"   (Density-Aware Transfer, a={NEARBY_NOTE_LIMIT})")
    print("=" * 50)

    batch_convert()


if __name__ == '__main__':
    main()
