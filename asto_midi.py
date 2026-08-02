#!/usr/bin/env python3
'''
Astonishia Story

Extracts General MIDI (GM) BGM tracks from Astonishia Story (1994).

Unpacking and File Conversion Structure:
Reads BGMMIDI.SON from the specified directory. The file contains a 16-bit
song count and an offset table pointing to MUSIC FILE blocks. Each MUSIC FILE
block contains header metadata followed by event streams using variable-length
quantity (VLQ) delta times and packed MIDI status bytes. These events are parsed
and converted into Standard MIDI Format (SMF Type 0) files.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Astonishia Story (1994) [GM]"

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


def read_vlq(data, pos, limit):
    """Read a standard MIDI variable-length quantity."""
    value = 0
    for _ in range(4):
        if pos >= limit:
            raise ValueError("truncated variable-length quantity")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos
    raise ValueError("variable-length quantity exceeds four bytes")


def write_vlq(value):
    if not 0 <= value <= 0x0FFFFFFF:
        raise ValueError(f"MIDI delta time out of range: {value}")
    encoded = [value & 0x7F]
    value >>= 7
    while value:
        encoded.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(encoded))


def song_entries(son):
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


def convert_song(son, start, end):
    """Return (ticks_per_quarter, one MIDI track's data) for a MUSIC FILE."""
    block = son[start:end]
    if len(block) < 26 or not block.startswith(MAGIC):
        raise ValueError(f"0x{start:X}: MUSIC FILE header not found")

    # The big-endian length at 0x16 includes byte 0x18, and excludes the
    # first 24 bytes of the header.  It is useful as an integrity check.
    declared_size = int.from_bytes(block[0x16:0x18], "big")
    if declared_size != len(block) - 24:
        raise ValueError(f"0x{start:X}: size field is inconsistent")
    division = block[0x11]
    if division == 0:
        raise ValueError(f"0x{start:X}: invalid tick division")

    pos, limit = 24, len(block)
    midi = bytearray()
    while True:
        delta, pos = read_vlq(block, pos, limit)
        if pos >= limit:
            raise ValueError(f"0x{start:X}: missing event after delta time")
        kind = block[pos]
        pos += 1
        if kind == 0xFF:                 # SON end-of-song marker
            midi += write_vlq(delta) + b"\xFF\x2F\x00"
            break

        family, channel = kind >> 4, kind & 0x0F
        if family == 1:                  # note on (velocity zero is note off)
            size, status = 2, 0x90 | channel
        elif family == 2:                # control change
            size, status = 2, 0xB0 | channel
        elif family == 3:                # program change
            size, status = 1, 0xC0 | channel
        elif family == 4:                # pitch bend, LSB then MSB
            size, status = 2, 0xE0 | channel
        elif kind == 0x80:               # three-byte, big-endian tempo
            size, status = 3, None
        else:
            raise ValueError(
                f"0x{start + pos - 1:X}: unknown SON event 0x{kind:02X}"
            )
        if pos + size > limit:
            raise ValueError(f"0x{start:X}: truncated event")
        payload = block[pos:pos + size]
        pos += size
        midi += write_vlq(delta)
        if status is None:
            midi += b"\xFF\x51\x03" + payload
        else:
            midi += bytes([status]) + payload

    if pos != limit:
        raise ValueError(f"0x{start:X}: {limit - pos} bytes after end marker")
    return division, bytes(midi)


def make_smf(division, track):
    return b"MThd" + struct.pack(">IHHH", 6, 0, 1, division) + b"MTrk" + struct.pack(">I", len(track)) + track


def extract_songs(input_dir, output_dir):
    """Extract songs from BGMMIDI.SON and save them using LOOKUP_TABLE filenames."""
    if input_dir.is_file():
        son_path = input_dir
    else:
        son_path = input_dir / "BGMMIDI.SON"
        if not son_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "BGMMIDI.SON":
                    son_path = child
                    break

    if not son_path.exists():
        raise FileNotFoundError(f"BGMMIDI.SON not found in {input_dir}")

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    son = son_path.read_bytes()
    entries = song_entries(son)
    offsets = [offset for offset, _ in entries] + [len(son)]

    used_filenames = set()

    for number, ((start, song_id), end) in enumerate(zip(entries, offsets[1:])):
        division, track = convert_song(son, start, end)

        if number in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[number]
        else:
            base_name = f"{number:02d}_{song_id:04X}"

        if not base_name.lower().endswith(".mid"):
            filename = f"{base_name}.MID"
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

        out_path = target_dir / filename
        out_path.write_bytes(make_smf(division, track))
        print(f"{filename}: {len(track)} MIDI bytes, division {division}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing BGMMIDI.SON (default: current directory)",
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