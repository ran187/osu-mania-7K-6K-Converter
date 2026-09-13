#!/usr/bin/env python3
"""
osu!mania 7K → 6K 谱面转换器（密度感知的第 4 列转移）
====================================================

将 7K 谱面转换为 6K。第 1/2/3/5/6/7 列直接重映射并保留
（长条保持长条）；只有第 4 列（0 基索引 3）会被移除或转移：

- 与其它音符同时间出现 → 直接丢弃
- 单独出现 → 密度检查：统计 ±250 ms（0.5 s 窗口）内起始的
  其它音符数
  - 数量 ≤ a（NEARBY_NOTE_LIMIT，默认 6）→ 进入间距检查
  - 数量 > a → 丢弃：附近密集，丢弃以保护其它列的手感
  （±250 ms 窗口内出现 7 个以上其它音符，即约每秒 14 个
  音符以上，视为密集）
- 通过密度检查 → 间距检查：分别求 K 到 6 个目标列最近按键的
  距离（普通音符为点，长条为其起止区间），取 6 列最大值 d；
  d ≥ MIN_TRANSFER_GAP（125 ms）才转移，否则丢弃

转换规则：
  1. 元数据：Creator 追加 "&ssaj"，Version 追加 "_726k"，
     BeatmapID 置 0，CircleSize 置 6。
  2. [HitObjects]：
     - 列 1/2/3/5/6/7（0 基：0,1,2,4,5,6）：重映射到 6K 列
       （列 < 3 不动，列 > 3 减 1），长条保持长条。
     - 列 4（0 基：3）：按上述规则转移或丢弃；转移目标为 6 列
       中最近按键距离最大、且 d ≥ MIN_TRANSFER_GAP 的列
       （并列按固定种子随机，见 resolve_transfers）；一律变为
       普通音符。
  3. 输出新 .osu 文件，文件名加 "_[726k]" 后缀；原文件不动。
"""

import bisect
import os
import random
import re

# ====================== 常量 ======================

ORIGINAL_KEYS = 7          # 输入 7K
TARGET_KEYS = 6            # 输出 6K
DELETED_COL = 3            # 被移除的列（0 基索引，即"第 4 列"）
TYPE_NORMAL = 1            # 普通音符的 type
TYPE_HOLD = 128            # mania 长条掩码
NEW_COMBO_FLAG = 4         # new-combo 标志位（type 字段第 2 位）

# 密度检查窗口的半宽（ms）：统计第 4 列单独音符 K 在
# ±DENSITY_WINDOW（0.5 s 窗口）内起始的其它音符数（can_transfer）。
DENSITY_WINDOW = 250

# 密度阈值（参数 a）：第 4 列单独音符在 ±DENSITY_WINDOW 窗口内
# 起始的其它音符超过该数量时丢弃，否则进入间距检查。
# 实际测试把这个值设为6，调得越大，转谱的结果越卡手
# 如果设置成-1，结果等于“直接删除第4轨道"
NEARBY_NOTE_LIMIT = 6   

# 间距阈值（ms）：K 到 6 个目标列各自最近按键的距离（到点或
# 区间）取最大值 d，d 仍小于该值 → 所有列都太挤，放弃转移。
MIN_TRANSFER_GAP = 125

# 并列选择用的固定随机种子：保证同一谱面重复转换时并列选择
# 结果一致，两次生成的谱面完全相同。
RANDOM_SEED = 114514


# ====================== 辅助函数 ======================

def get_column(x, key_count):
    """由 x 坐标求 0 基列号（osu! 规范：floor(x * keyCount / 512)）。"""
    return int(x * key_count / 512)


def get_new_x(col, key_count=TARGET_KEYS):
    """目标键数下某列中心的 x 坐标：floor((col + 0.5) * 512 / key_count)。"""
    return int((col + 0.5) * 512 / key_count)


# ====================== HitObject 解析 ======================

def parse_hit_object(line):
    """
    解析一行 [HitObjects]。

    支持两种格式：
      - 普通：x, y, time, type, hitSound, hitSample
      - 长条：x, y, time, type, hitSound, endTime:hitSample

    返回 dict；无效行返回 None。
    """
    line = line.strip()
    if not line:
        return None

    parts = line.split(',')
    if len(parts) < 5:      # 至少 x,y,time,type,hitsound
        return None

    try:
        x = int(parts[0])
        y = int(parts[1])
        time = int(parts[2])
        obj_type = int(parts[3])
        hit_sound = int(parts[4])
    except ValueError:
        return None

    is_long = bool(obj_type & TYPE_HOLD)               # 含 128 位 → 长条
    is_new_combo = bool(obj_type & NEW_COMBO_FLAG)     # 含 4 位 → new-combo
    end_time = None
    hit_sample = ''

    # 第 6 个字段起的内容取决于是否为长条
    remainder = ','.join(parts[5:])

    if is_long:
        # 长条格式：endTime:hitSample
        colon_idx = remainder.find(':')
        if colon_idx != -1:
            try:
                end_time = int(remainder[:colon_idx])
            except ValueError:
                end_time = 0
            hit_sample = remainder[colon_idx + 1:]
        else:
            # 无冒号的退化情况：整个 remainder 当作 endTime
            try:
                end_time = int(remainder)
            except ValueError:
                end_time = 0
            hit_sample = ''
    else:
        # 普通音符：remainder 就是 hitSample
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
    把音符 dict 序列化回 .osu 行。

    - 普通音符：type 为 1；若为谱面第一个音符（ensure_new_combo）
      或原有 new-combo 标志则为 5。
    - 长条：type 恒为 128，不应用 new-combo（即使是第一个音符）；
      尾部用 "endTime:hitSample" 格式（mania 谱面惯例）。
    """
    if obj.get('is_long'):
        # 长条：type 恒为 128
        t = TYPE_HOLD
    else:
        # 普通音符：首个音符或原有 new-combo 时为 5
        is_new_combo = ensure_new_combo or obj.get('is_new_combo')
        t = TYPE_NORMAL | NEW_COMBO_FLAG if is_new_combo else TYPE_NORMAL

    if obj.get('is_long') and obj.get('endTime') is not None:
        # 长条：endTime:hitSample
        return (f"{obj['x']},{obj['y']},{obj['time']},"
                f"{t},{obj['hitSound']},{obj['endTime']}:{obj['hitSample']}")
    else:
        # 普通音符
        return (f"{obj['x']},{obj['y']},{obj['time']},"
                f"{t},{obj['hitSound']},{obj['hitSample']}")


# ====================== 分组 ======================

def read_groups(hit_object_lines):
    """
    按起始时间分组产出音符。

    每个元素：(time_in_ms, [音符 dict 列表])
    [HitObjects] 按时间排序，故只需比较相邻行。
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


# ====================== 转移解析 ======================

def can_transfer(t, all_start_times, limit=NEARBY_NOTE_LIMIT):
    """
    密度检查：第 4 列单独音符（时间 t）能否转移？

    统计 ±DENSITY_WINDOW（0.5 s 窗口）内其它音符的起始数：
      - count ≤ limit → True（稀疏 → 进入间距检查）
      - count > limit → False（密集 → 丢弃，保护手感）

    all_start_times 为全体音符起始时间的有序列表（含第 4 列），
    两次二分查找即可得窗口内数量（O(log N)）。候选音符在其时间
    点上仅此一个，故计数减 1。
    """
    lo = bisect.bisect_left(all_start_times, t - DENSITY_WINDOW)
    hi = bisect.bisect_right(all_start_times, t + DENSITY_WINDOW)
    nearby = (hi - lo) - 1          # 窗口内音符数减去候选自身
    return nearby <= limit


def resolve_transfers(transfer_candidates, col_spans, rng=None):
    """
    为每个通过密度检查的候选挑选目标 6K 列。

    密度资格已在调用前由 can_transfer 判定；本函数做间距检查并
    选目标列：6 列中 K 到最近按键距离的最大值 d 必须
    ≥ MIN_TRANSFER_GAP，否则候选在所有列上都离已有按键太近，
    丢弃。

    点 / 区间模型
    -------------
    每个非第 4 列音符是一个点或区间：
      - 普通音符 T   → 点 T
      - 长条 S..E    → 区间 [S, E]

    对候选时间 T，在 6 个目标列上分别计算 T 到最近按键的距离：
      - T 在区间内（含端点）→ 0
      - T 在点/区间左侧      → start - T
      - T 在点/区间右侧      → T - end
      - 列为空               → +∞

    取 6 列距离的最大值 d（空隙最大，对其它列打扰最小）；空列
    （+∞）恒胜，因此稀疏段会落到空闲列上。并列时按固定种子随机
    选一列（同一谱面多次转换结果一致；rng 未传入时退回模块级
    random）。若 d < MIN_TRANSFER_GAP，所有列都太挤，丢弃候选。
    转移后的音符一律为普通音符（type 1）。

    返回新音符 dict 列表（含目标列的 x 坐标）；未通过间距检查
    的候选被省略。
    """
    if rng is None:
        rng = random                  # 未显式传入时退回模块级随机源

    # 每列按起点有序的点/区间表，以及"前 i 项 end 的最大值"前缀表
    col_starts = [[s for s, _ in spans] for spans in col_spans]
    col_end_prefix = []
    for spans in col_spans:
        prefix = []
        best_end = -float('inf')
        for _s, e in spans:
            best_end = max(best_end, e)
            prefix.append(best_end)
        col_end_prefix.append(prefix)

    resolved = []

    for obj in transfer_candidates:
        T = obj['time']
        best_cols = []
        best_d = -1.0                   # 迄今各列最近距离的最大值（≥ 0）

        for col in range(TARGET_KEYS):
            starts = col_starts[col]

            if not starts:
                # 空列 —— 理想选择
                d = float('inf')
            else:
                idx = bisect.bisect_right(starts, T)

                # T 之后最近的按键
                d_left = starts[idx] - T if idx < len(starts) else float('inf')

                # T 之前（含）开始的点/区间：若某区间覆盖 T 则距离为 0，
                # 否则最近距离为 T 减这些项的最大 end
                if idx > 0:
                    max_end = col_end_prefix[col][idx - 1]
                    d_right = 0.0 if max_end >= T else T - max_end
                else:
                    d_right = float('inf')

                d = min(d_left, d_right)

            # ---- 记录跨列的最大最近距离 ----
            if d > best_d:
                best_d = d
                best_cols = [col]
            elif d == best_d:
                best_cols.append(col)

        # ---- 间距检查：最佳列仍比 MIN_TRANSFER_GAP 更近 → 丢弃 ----
        if best_d < MIN_TRANSFER_GAP:
            continue

        # ---- 选定目标列 ----
        target_col = rng.choice(best_cols)
        new_x = get_new_x(target_col)

        new_obj = {
            'x': new_x,
            'y': obj['y'],
            'time': obj['time'],
            'hitSound': obj['hitSound'],
            'hitSample': obj['hitSample'],
            'is_new_combo': obj['is_new_combo'],
            'is_long': False,           # 第 4 列音符一律变普通音符
            'endTime': None,
        }
        resolved.append(new_obj)

    return resolved


# ====================== 转换核心 ======================

def convert_hit_objects(hit_object_lines):
    """
    将 [HitObjects] 段从 7K 转 6K。三阶段算法：

      阶段 1 — 按时间顺序单遍扫描各分组：
        - 非第 4 列：重映射列与 x，长条保留，加入输出池；
          同时按列构建点/区间表（普通音符为点、长条为起止
          区间，供间距检查用）
        - 第 4 列单独：加入转移候选
        - 第 4 列有伴：静默丢弃
        - 收集全体音符起始时间的有序表（供密度检查二分查找）

      阶段 2 — 密度过滤并解析转移（按时间序）：
        - can_transfer 过滤：±250 ms 窗口内起始的其它音符 ≤ a
          （a = NEARBY_NOTE_LIMIT）才保留
        - 幸存候选交给 resolve_transfers：K 到各列最近按键
          距离的最大值 d ≥ MIN_TRANSFER_GAP 才转移，目标列为
          d 所在列（并列随机）

      阶段 3 — 合并常规与转移音符，按 (time, x) 排序，
               用 format_hit_object 序列化。
    """
    # 各列音符点/区间表（按起点排序）：
    #   普通音符 T → (T, T)
    #   长条 S..E  → (S, E)
    col_spans = [[] for _ in range(TARGET_KEYS)]

    # 全体音符起始时间的有序表（含第 4 列），供密度检查二分查找
    all_start_times = []

    regular_notes = []          # 直接重映射的输出音符
    transfer_candidates = []    # 第 4 列单独音符（已按时间序）

    # ---- 阶段 1 --------------------------------------------------------
    for _time, group in read_groups(hit_object_lines):

        # 标注每个音符的 7K 列号
        cols_7k = set()
        for obj in group:
            c = get_column(obj['x'], ORIGINAL_KEYS)
            obj['_col_7k'] = c
            cols_7k.add(c)

        other_cols = cols_7k - {DELETED_COL}
        is_col3_alone = (DELETED_COL in cols_7k) and (not other_cols)

        for obj in group:
            col = obj['_col_7k']

            # ---- 记录全局起始时间 ----
            bisect.insort(all_start_times, obj['time'])

            if col == DELETED_COL:
                if is_col3_alone:
                    transfer_candidates.append(obj)
                # else: 第 4 列有伴 → 丢弃
            else:
                # 重映射：列 < 3 不动，列 > 3 减 1
                new_col = col - 1 if col > DELETED_COL else col
                new_x = get_new_x(new_col)

                new_obj = {
                    'x': new_x,
                    'y': obj['y'],
                    'time': obj['time'],
                    'hitSound': obj['hitSound'],
                    'hitSample': obj['hitSample'],
                    'is_new_combo': obj['is_new_combo'],
                    'is_long': obj['is_long'],          # 长条保留
                    'endTime': obj['endTime'],          # 结尾时间保留
                }
                regular_notes.append(new_obj)

                # ---- 构建点/区间 ----
                if obj['is_long'] and obj['endTime'] is not None:
                    span = (obj['time'], obj['endTime'])
                else:
                    span = (obj['time'], obj['time'])

                # 按起点有序插入
                ins_idx = bisect.bisect_left(col_spans[new_col], span)
                col_spans[new_col].insert(ins_idx, span)

    # ---- 阶段 2 --------------------------------------------------------
    # 密度过滤：窗口内起始的其它音符数 ≤ a 才保留
    eligible_candidates = [
        obj for obj in transfer_candidates
        if can_transfer(obj['time'], all_start_times)
    ]
    # 固定种子随机源：同一谱面重复转换，并列选择结果完全一致
    rng = random.Random(RANDOM_SEED)
    resolved_notes = resolve_transfers(eligible_candidates, col_spans, rng)

    # ---- 阶段 3 --------------------------------------------------------
    all_notes = regular_notes + resolved_notes
    all_notes.sort(key=lambda o: (o['time'], o['x']))

    output = []
    is_first = True
    for obj in all_notes:
        output.append(format_hit_object(obj, ensure_new_combo=is_first))
        is_first = False

    return output


# ====================== 文件级转换 ======================

def _modify_creator(line):
    """Creator 值追加 '&ssaj'。"""
    m = re.match(r'(Creator\s*:\s*)(.*)', line)
    if m:
        return f"{m.group(1)}{m.group(2).rstrip()}&ssaj\n"
    return line


def _modify_version(line):
    """Version 值追加 '_726k'。"""
    m = re.match(r'(Version\s*:\s*)(.*)', line)
    if m:
        return f"{m.group(1)}{m.group(2).rstrip()}_726k\n"
    return line


def _modify_beatmap_id(line):
    """BeatmapID 置 0。"""
    return re.sub(r'(BeatmapID\s*:\s*)\d+', r'\g<1>0', line)


def _modify_circle_size(line):
    """CircleSize 置 6。"""
    return re.sub(r'(CircleSize\s*:\s*)\d+', r'\g<1>6', line)


def is_mania_7k(osu_path):
    """
    判断 *osu_path* 是否为 mania 7K 谱面。

    逐行读取并尽早停止（Mode 与 CircleSize 总在前约 40 行内）。
    检查：Mode=3（osu!mania）、CircleSize=7（7 键）。
    """
    mode = None
    circle_size = None

    try:
        with open(osu_path, 'r', encoding='utf-8-sig') as f:
            for line in f:
                stripped = line.strip()

                # ---- 到达 [HitObjects] 即可停止 ----
                if stripped == '[HitObjects]':
                    break

                # ---- Mode（[General] 段）----
                if mode is None and stripped.startswith('Mode:'):
                    try:
                        mode = int(stripped.split(':')[1].strip())
                    except ValueError:
                        pass
                    if mode != 3:
                        return False

                # ---- CircleSize（[Difficulty] 段）----
                if circle_size is None and stripped.startswith('CircleSize:'):
                    try:
                        circle_size = int(stripped.split(':')[1].strip())
                    except ValueError:
                        pass
                    if circle_size != 7:
                        return False

                # 两项都确定后无需继续读
                if mode is not None and circle_size is not None:
                    break

    except Exception:
        return False

    return mode == 3 and circle_size == 7


def convert_osu_file(osu_path):
    """
    转换单个 .osu 文件（7K → 6K）。

    返回新文件路径，失败返回 None。原文件不会被修改。
    """
    # ---- 读取原文件 ----
    try:
        with open(osu_path, 'r', encoding='utf-8-sig') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"    ERROR reading file: {e}")
        return None

    # ---- 定位 [HitObjects] ----
    hit_objects_header_idx = None
    raw_hit_object_lines = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == '[HitObjects]':
            hit_objects_header_idx = i
            continue
        if hit_objects_header_idx is not None:
            if stripped.startswith('['):
                break          # 遇到下一节（正常不会有，稳妥起见）
            if stripped:
                raw_hit_object_lines.append(stripped)

    if hit_objects_header_idx is None:
        print(f"    No [HitObjects] section — skipping")
        return None
    if not raw_hit_object_lines:
        print(f"    [HitObjects] is empty — skipping")
        return None

    # ---- 转换 HitObjects ----
    converted_hos = convert_hit_objects(raw_hit_object_lines)

    # ---- 组装输出 ----
    output_lines = []

    for line in lines:
        stripped = line.strip()

        if stripped == '[HitObjects]':
            # [HitObjects] 按规范恒为最后一节，写入转换结果后即可停止
            output_lines.append(line)
            for ho in converted_hos:
                output_lines.append(ho + '\n')
            output_lines.append('\n')
            break

        # ---- 在 [HitObjects] 之前应用元数据补丁 ----
        if stripped.startswith('Creator:'):
            line = _modify_creator(line)
        elif stripped.startswith('Version:'):
            line = _modify_version(line)
        elif stripped.startswith('BeatmapID:'):
            line = _modify_beatmap_id(line)
        elif stripped.startswith('CircleSize:'):
            line = _modify_circle_size(line)

        output_lines.append(line)

    # ---- 写入新文件 ----
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


# ====================== 批量模式 ======================

def get_songs_dir():
    """
    返回 osu! Songs 目录（为当前用户硬编码）。

    换机器运行请改成你自己的 Songs 路径：osu! → Options →
    "Open osu! folder"，进入其中的 Songs 子目录。
    """
    return r'C:\Users\SmdSa\AppData\Local\osu!\Songs'


def batch_convert():
    """遍历 osu! Songs 目录，找出 7K mania 谱面，逐个生成 6K 副本。"""
    songs_dir = get_songs_dir()

    if not songs_dir:
        print("Could not auto-detect the osu! Songs folder.")
        print("Please paste the full path to your Songs folder:")
        songs_dir = input().strip().strip('"')
        if not os.path.isdir(songs_dir):
            print(f"'{songs_dir}' is not a valid directory.  Exiting.")
            return

    print(f"\nSongs folder: {songs_dir}\n")

    # 收集子文件夹（每个 = 一个谱面集）
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
        # 进度：已完成 / 总数
        print(f"[{idx}/{total}]  {entry}")

        # 收集 .osu 文件
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
                continue          # 非 7K mania —— 静默跳过

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


# ====================== 主程序 ======================

def main():
    print("=" * 50)
    print("   osu!mania  7K  -->  6K  Beatmap Converter")
    print(f"   (Density-Aware Transfer, a={NEARBY_NOTE_LIMIT})")
    print("=" * 50)

    batch_convert()


if __name__ == '__main__':
    main()
