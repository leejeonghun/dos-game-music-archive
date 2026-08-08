#!/usr/bin/env python3
'''
Astonishia Story

Extracts AdLib FM BGM tracks (.ROL) from Astonishia Story (1994).

Unpacking and File Conversion Structure:
Reads ADLIB.SON from the specified directory. The file contains a 16-bit
song count and an offset table pointing to MUSIC FILE blocks. Each MUSIC FILE
block contains header metadata followed by event streams using variable-length
quantity (VLQ) delta times and packed status bytes. These events are parsed
and converted into Standard AdLib ROL format files.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Astonishia Story (1994) [FM]"

LOOKUP_TABLE = {
    0: 'A1',
    1: 'AS#44',
    2: 'ASBAL',
    3: 'ASBANJO',
    4: 'ASGTP1',
    5: 'ASHOP-P1',
    6: 'ASHORE',
    7: 'ASMOR',
    8: 'ASNO#1',
    9: 'B3-1',
    10: 'BATTLE',
    11: 'DM',
    12: 'BGM4',
    13: 'CASTLE',
    14: 'DEARME',
    15: 'EVENT1',
    16: 'EVENT2',
    17: 'EVENT3',
    18: 'EVENT5-1',
    19: 'FAV2',
    20: 'FUN',
    21: 'BATTLE',
    22: 'KOBOE',
    23: 'MAL',
    24: 'MARCH',
    25: 'MUDO-1',
    26: 'ORC-1',
    27: 'PS',
    28: 'SEA-1',
    29: 'FD',
    30: 'LV2',
    31: 'LV4',
    32: 'ITEM1',
    33: 'GAMEOVER',
    34: 'DUN-1',
    35: 'SLEEP',
    36: 'TAJI',
    37: 'TEM-1',
    38: 'INT',
    39: 'LOGO',
    40: 'DANGER',
}

MAGIC = b"MUSIC FILE\0"
NUM_VOICES = 11
NAME_RECORD_SIZE = 9
TICK_SCALE = 10


# --------------------------------------------------------------------------- #
# Song Table Parsing
# --------------------------------------------------------------------------- #

def song_entries(son: bytes) -> list[tuple[int, int]]:
    """Parse song count and offset table from ADLIB.SON."""
    if len(son) < 2:
        raise ValueError("file is too short for the song table")
    count = struct.unpack_from("<H", son)[0]
    table_end = 2 + count * 6
    if table_end > len(son):
        raise ValueError("truncated song table")
    entries = [struct.unpack_from("<IH", son, 2 + i * 6) for i in range(count)]
    offsets = [offset for offset, _ in entries]
    if offsets != sorted(offsets) or any(offset < table_end for offset in offsets):
        raise ValueError("invalid song offsets")
    return entries


# --------------------------------------------------------------------------- #
# Block Parsing
# --------------------------------------------------------------------------- #

def read_vlq(block: bytes, pos: int) -> tuple[int, int]:
    """Read a variable-length quantity (VLQ) delta time."""
    value = 0
    while True:
        b = block[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return value, pos


def parse_block(block: bytes) -> dict | None:
    """Parse a MUSIC FILE block into metadata and channel event streams."""
    if not block.startswith(MAGIC):
        raise ValueError("MUSIC FILE header not found")

    tick_beat = block[17] if len(block) > 17 else 3
    flags_byte13 = block[13] if len(block) > 13 else 1

    n_names = block[19]
    pos = 20
    names = []
    for _ in range(n_names):
        raw = block[pos:pos + NAME_RECORD_SIZE]
        pos += NAME_RECORD_SIZE
        names.append(raw.split(b"\0", 1)[0].decode("ascii", "replace"))

    events: list[tuple[int, str, int | None, int | None]] = []
    tick = 0
    n = len(block)
    default_tempo_code: int | None = None

    while pos < n:
        status = block[pos]
        pos += 1
        if status & 0x80:
            if status == 0xFF:
                return {
                    "names": names,
                    "events": events,
                    "total_ticks": tick,
                    "tick_beat": tick_beat,
                    "flags_byte13": flags_byte13,
                    "default_tempo_code": default_tempo_code,
                }
            elif status == 0xFE:
                pass
            elif status == 0x80:
                if pos + 3 > n:
                    return None
                value = int.from_bytes(block[pos:pos + 3], "big")
                pos += 3
                events.append((tick, "tempo", None, value))
            elif status == 0xF0:
                return None
            else:
                if default_tempo_code is None:
                    default_tempo_code = status
        else:
            family = (status >> 4) & 0x07
            channel = status & 0x0F
            if family == 0:
                events.append((tick, "note_off", channel, None))
            elif family == 1:
                if pos >= n:
                    return None
                note = block[pos]
                pos += 1
                events.append((tick, "note_on", channel, note))
            elif family == 2:
                if pos >= n:
                    return None
                vol = block[pos]
                pos += 1
                events.append((tick, "volume", channel, vol))
            elif family == 3:
                if pos >= n:
                    return None
                idx = block[pos]
                pos += 1
                events.append((tick, "timbre", channel, idx))
            elif family == 4:
                if pos + 2 > n:
                    return None
                value = int.from_bytes(block[pos:pos + 2], "big")
                pos += 2
                events.append((tick, "pitch", channel, value))
        if pos >= n:
            break
        try:
            delta, pos = read_vlq(block, pos)
        except IndexError:
            return None
        tick += delta

    return None


# --------------------------------------------------------------------------- #
# ROL Formatting & Conversion
# --------------------------------------------------------------------------- #

def _cstr(s: str, size: int) -> bytes:
    b = s.encode("ascii", "replace")[: size - 1]
    return b + b"\0" * (size - len(b))


def _f32(value: float) -> bytes:
    return struct.pack("<f", value)


def _round_volume(raw: int) -> float:
    """Convert raw volume (0-126) to percentage float rounded to 2 decimals."""
    return round((max(min(raw, 126), 0) / 126.0) * 100) / 100.0


def _build_voice_track(note_pts: list[tuple[int, int]], total_ticks: int) -> list[tuple[int, int]]:
    """Build consecutive (note, duration) sequence for voice track."""
    pts = sorted(note_pts, key=lambda e: e[0])
    if not pts:
        return []
    result: list[tuple[int, int]] = []
    if pts[0][0] > 0:
        result.append((0, pts[0][0]))
    for i, (t, note) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else total_ticks
        dur = nxt - t
        if dur > 0:
            result.append((note, dur))
    return result


def _default_mult_from_code(code: int | None) -> float:
    """Calculate default tempo multiplier from single-byte code."""
    if code is None:
        return 1.0
    raw = code * 256
    return round((50000.0 / raw) * 20) / 20.0


def build_rol(parsed: dict, bnk_index: dict[str, int] | None = None) -> bytes:
    """Convert parsed block data into Standard .ROL file format bytes."""
    names = parsed["names"]
    events = parsed["events"]
    tick_beat = parsed.get("tick_beat", 3)

    timbre_ticks = [t for t, k, c, v in events if k == "timbre"]
    global_zero = min(timbre_ticks) if timbre_ticks else 0
    song_total_ticks = (parsed["total_ticks"] - global_zero) // TICK_SCALE

    by_channel: dict[int, list[tuple[int, str, int]]] = {}
    tempo_events: list[tuple[int, int | None]] = []
    for tick, kind, channel, value in events:
        if tick < global_zero:
            continue
        if kind == "tempo":
            tempo_events.append((tick - global_zero, value))
            continue
        if channel is None or channel >= NUM_VOICES:
            continue
        by_channel.setdefault(channel, []).append(((tick - global_zero) // TICK_SCALE, kind, value))

    voice_tracks, timbre_tracks, volume_tracks, pitch_tracks, ch_total_ticks = [], [], [], [], []
    for ch in range(NUM_VOICES):
        chan_events = by_channel.get(ch, [])
        note_events = [(t, k, v) for t, k, v in chan_events if k in ("note_on", "note_off")]
        if note_events:
            last_t, last_k, _ = note_events[-1]
            total_ticks = song_total_ticks if last_k == "note_on" else last_t
        else:
            total_ticks = 0
        ch_total_ticks.append(total_ticks)

        note_pts = [(t, (v if k == "note_on" else 0)) for t, k, v in note_events]
        voice_tracks.append(_build_voice_track(note_pts, total_ticks))

        timbre_list = sorted((t, v) for t, k, v in chan_events if k == "timbre")
        if total_ticks > 0 and (not timbre_list or timbre_list[0][0] != 0):
            timbre_list.insert(0, (0, 0))
        timbre_tracks.append(timbre_list)

        volume_tracks.append(sorted((t, v) for t, k, v in chan_events if k == "volume"))
        pitch_tracks.append(sorted((t, v) for t, k, v in chan_events if k == "pitch"))

    basic_tempo = 120.0
    default_tempo_code = parsed.get("default_tempo_code")
    if not tempo_events:
        tempo_events = [(0, None)]

    def _tempo_mult(raw_value: int | None) -> float:
        if raw_value is None:
            return _default_mult_from_code(default_tempo_code)
        if raw_value <= 0:
            return 1.0
        return round((50000.0 / raw_value) * 20) / 20.0

    out = bytearray()
    out += struct.pack("<HH", 0, 4)
    out += _cstr("\\roll\\default", 40)

    flags_byte13 = parsed.get("flags_byte13", 1)
    is_melodic = 1 if flags_byte13 == 0 else 0
    if tick_beat < 6 and flags_byte13:
        scale_y, scale_x = 48, 56
    else:
        scale_y, scale_x = 72, 70

    out += struct.pack("<HHHH", tick_beat, 4, scale_y, scale_x)
    out += bytes([0])
    out += bytes([is_melodic])

    counters = list(ch_total_ticks)
    counters += [len(t) for t in timbre_tracks]
    counters += [len(t) for t in volume_tracks]
    counters += [len(t) for t in pitch_tracks]
    counters += [len(tempo_events)]
    out += struct.pack("<45H", *counters)
    out += bytes(38)

    out += _cstr("Tempo", 15)
    out += _f32(basic_tempo)
    out += struct.pack("<H", len(tempo_events))
    for tick, raw_value in tempo_events:
        mult = _tempo_mult(raw_value)
        out += struct.pack("<H", tick & 0xFFFF) + _f32(mult)

    for ch in range(NUM_VOICES):
        out += _cstr(f"Voix {ch:2d}", 15)
        out += struct.pack("<H", ch_total_ticks[ch] & 0xFFFF)
        for note, dur in voice_tracks[ch]:
            out += struct.pack("<HH", note & 0xFFFF, dur & 0xFFFF)

        out += _cstr(f"Timbre {ch:2d}", 15)
        out += struct.pack("<H", len(timbre_tracks[ch]))
        for tick, idx in timbre_tracks[ch]:
            name = names[idx] if 0 <= idx < len(names) else ""
            real_idx = bnk_index.get(name.lower(), idx) if bnk_index else idx
            out += struct.pack("<H", tick & 0xFFFF)
            out += _cstr(name, 9)
            out += bytes([0])
            out += struct.pack("<H", real_idx & 0xFFFF)

        out += _cstr(f"Volume {ch:2d}", 15)
        out += struct.pack("<H", len(volume_tracks[ch]))
        for tick, value in volume_tracks[ch]:
            out += struct.pack("<H", tick & 0xFFFF)
            out += _f32(_round_volume(value))

        out += _cstr(f"Pitch {ch:2d}", 15)
        out += struct.pack("<H", len(pitch_tracks[ch]))
        for tick, value in pitch_tracks[ch]:
            out += struct.pack("<H", tick & 0xFFFF)
            out += _f32(round((value / 8192.0) * 20) / 20.0 if value else 1.0)

    return bytes(out)


def load_bnk_index(path: Path) -> dict[str, int]:
    """Parse BNK instrument bank file to map instrument names to global indices."""
    data = path.read_bytes()
    num_instruments = struct.unpack_from("<H", data, 10)[0]
    offset_name = struct.unpack_from("<I", data, 12)[0]
    result = {}
    pos = offset_name
    for _ in range(num_instruments):
        index = struct.unpack_from("<H", data, pos)[0]
        name = data[pos + 3:pos + 3 + 9].split(b"\0", 1)[0].decode("ascii", "replace")
        result[name.lower()] = index
        pos += 12
    return result


def find_bnk_file(search_dir: Path) -> Path | None:
    """Find any .BNK file in the input directory if present."""
    if search_dir.is_file():
        search_dir = search_dir.parent
    for child in search_dir.iterdir():
        if child.suffix.upper() == ".BNK":
            return child
    return None


# --------------------------------------------------------------------------- #
# Extraction & Renaming Logic
# --------------------------------------------------------------------------- #

def extract_songs(input_dir: Path, output_dir: Path) -> None:
    """Extract songs from ADLIB.SON and save them using LOOKUP_TABLE filenames."""
    if input_dir.is_file():
        son_path = input_dir
    else:
        son_path = input_dir / "ADLIB.SON"
        if not son_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "ADLIB.SON":
                    son_path = child
                    break

    if not son_path.exists():
        raise FileNotFoundError(f"ADLIB.SON not found in {input_dir}")

    game_dir = input_dir if input_dir.is_dir() else input_dir.parent

    bnk_path = find_bnk_file(game_dir)
    bnk_index = load_bnk_index(bnk_path) if bnk_path else None

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    son = son_path.read_bytes()
    entries = song_entries(son)
    offsets = [offset for offset, _ in entries] + [len(son)]

    used_filenames = set()

    for number, ((start, song_id), end) in enumerate(zip(entries, offsets[1:])):
        block = son[start:end]
        parsed = parse_block(block)
        if parsed is None:
            print(f"Song {number} (ID 0x{song_id:04X}): SKIPPED - unparseable block")
            continue

        if number in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[number]
        else:
            base_name = f"{number:02d}_{song_id:04X}"

        if not base_name.lower().endswith(".rol"):
            filename = f"{base_name}.ROL"
        else:
            filename = base_name

        if filename in used_filenames:
            stem = Path(filename).stem
            ext = Path(filename).suffix
            count = 2
            while f"{stem}_{count}{ext}" in used_filenames:
                count += 1
            filename = f"{stem}_{count}{ext}"

        used_filenames.add(filename)

        rol_data = build_rol(parsed, bnk_index)
        out_path = target_dir / filename
        out_path.write_bytes(rol_data)
        print(f"{filename}: {len(rol_data)} bytes ({len(parsed['names'])} instruments, {len(parsed['events'])} events)")

    # Copy A.BNK -> STANDARD.BNK if present in the game directory
    for child in game_dir.iterdir():
        if child.name.upper() == "A.BNK":
            (target_dir / "STANDARD.BNK").write_bytes(child.read_bytes())
            print(f"Copied {child.name} -> STANDARD.BNK")
            break


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing ADLIB.SON (default: current directory)",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Output directory (default: current directory)",
    )
    args = parser.parse_args()
    extract_songs(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()