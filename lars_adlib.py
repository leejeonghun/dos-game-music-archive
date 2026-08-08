#!/usr/bin/env python3
'''
Lars the Wanderer

Extracts AdLib (FM) BGM tracks (.SOP) from Lars the Wanderer (1995).

Unpacking and File Structure:
Reads LARS.RSC from the specified directory. The RSC archive header contains
a 16-bit file count and a 32-bit offset pointing to the resource entry table.
The entry table starts with a 9-byte magic header followed by 26-byte file entries
containing metadata (type, CP949-encoded filename, offset, size, and extra data).
Only files with the .SOP extension are parsed and saved to the output directory.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Lars the Wanderer (1995) [FM]"

LOOKUP_TABLE = {}


def parse_rsc(data):
    """Parse LARS.RSC header and table entries."""
    if len(data) < 0x1C:
        raise ValueError("File is too short to be a valid RSC archive")

    file_count = struct.unpack_from("<H", data, 0x16)[0]
    table_offset = struct.unpack_from("<I", data, 0x18)[0]

    if table_offset + 9 > len(data):
        raise ValueError("Table offset is outside file bounds")

    pos = table_offset + 9
    entries = []
    for _ in range(file_count):
        if pos + 0x1A > len(data):
            break
        raw = data[pos:pos + 0x1A]
        pos += 0x1A

        ftype = raw[0]
        name_bytes = raw[1:14].split(b"\x00")[0]
        name = name_bytes.decode("cp949", errors="ignore").strip()
        offset = struct.unpack_from("<I", raw, 0x0E)[0]
        size = struct.unpack_from("<I", raw, 0x12)[0]
        extra = struct.unpack_from("<I", raw, 0x16)[0]

        entries.append((ftype, name, offset, size, extra))

    return entries


def extract_sop(input_dir, output_dir):
    """Extract .SOP files from LARS.RSC and save them using LOOKUP_TABLE renaming."""
    if input_dir.is_file():
        rsc_path = input_dir
    else:
        rsc_path = input_dir / "LARS.RSC"
        if not rsc_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "LARS.RSC":
                    rsc_path = child
                    break

    if not rsc_path.exists():
        raise FileNotFoundError(f"LARS.RSC not found in {input_dir}")

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    data = rsc_path.read_bytes()
    entries = parse_rsc(data)

    used_filenames = set()
    sop_index = 0

    for ftype, name, offset, size, extra in entries:
        if not name or not name.upper().endswith(".SOP"):
            continue

        if offset + size > len(data):
            print(f"Skipping truncated file: {name}")
            continue

        file_bytes = data[offset:offset + size]

        # Renaming resolution via LOOKUP_TABLE (index or original filename)
        if sop_index in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[sop_index]
        elif name in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[name]
        else:
            base_name = name

        if not base_name.upper().endswith(".SOP"):
            filename = f"{base_name}.SOP"
        else:
            filename = base_name

        # Resolve duplicated filenames if any
        if filename in used_filenames:
            stem = Path(filename).stem
            ext = Path(filename).suffix
            count = 2
            while f"{stem}_{count}{ext}" in used_filenames:
                count += 1
            filename = f"{stem}_{count}{ext}"

        used_filenames.add(filename)

        out_path = target_dir / filename
        out_path.write_bytes(file_bytes)
        print(f"{filename}: {len(file_bytes)} bytes")
        sop_index += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing LARS.RSC (default: current directory)",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Output directory (default: current directory)",
    )
    args = parser.parse_args()
    extract_sop(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()