#!/usr/bin/env python3
'''
Empire of Angels II

Extracts AdLib FM (RIX) BGM tracks from Empire of Angels II (1994).

Unpacking and File Structure:
Reads MUSIC.SWF from the specified directory. The file starts with a header
table containing 6-byte entries (a 32-bit little-endian offset followed by a
16-bit little-endian size). Each offset points to an AdLib FM audio stream in
SoftStar RIX format, verified by the signature 0x55AA. Entries with offset
0xFFFFFFFF or invalid signatures indicate the end of the entry table. Valid
blocks are extracted and saved as Standard RIX (.RIX) files.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Empire of Angels II (1994) [FM]"

LOOKUP_TABLE = {}

RIX_SIGNATURE = b"\xAA\x55"
TABLE_ENTRY_SIZE = 6


def parse_entry_table(data):
    """Parse table entries from MUSIC.SWF header and return list of (offset, size)."""
    entries = []
    filesize = len(data)
    i = 0
    while True:
        pos = i * TABLE_ENTRY_SIZE
        if pos + TABLE_ENTRY_SIZE > filesize:
            break

        offset, size = struct.unpack_from("<IH", data, pos)

        if offset == 0xFFFFFFFF or size == 0 or offset >= filesize:
            break

        if offset + size > filesize:
            break

        if data[offset : offset + 2] != RIX_SIGNATURE:
            break

        entries.append((offset, size))
        i += 1

    return entries


def extract_rix(input_dir, output_dir):
    """Extract RIX songs from MUSIC.SWF and save them using LOOKUP_TABLE filenames."""
    if input_dir.is_file():
        swf_path = input_dir
    else:
        swf_path = input_dir / "MUSIC.SWF"
        if not swf_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "MUSIC.SWF":
                    swf_path = child
                    break

    if not swf_path.exists():
        raise FileNotFoundError(f"MUSIC.SWF not found in {input_dir}")

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    data = swf_path.read_bytes()
    entries = parse_entry_table(data)
    if not entries:
        raise ValueError("No valid RIX entries found in MUSIC.SWF")

    used_filenames = set()

    for idx, (offset, size) in enumerate(entries):
        chunk = data[offset : offset + size]

        if idx in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[idx]
        else:
            base_name = f"MUSIC_{idx:02d}"

        if not base_name.lower().endswith(".rix"):
            filename = f"{base_name}.RIX"
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
        out_path.write_bytes(chunk)
        print(f"{filename}: offset 0x{offset:06X}, size {size} bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing MUSIC.SWF (default: current directory)",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Output directory (default: current directory)",
    )
    args = parser.parse_args()
    extract_rix(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()