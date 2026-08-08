#!/usr/bin/env python3
'''
Fox Ranger (1992)

Extracts Roland Sound Canvas SC-55 (GS) MIDI tracks from Fox Ranger (1992).

Unpacking and File Conversion Structure:
Reads SC55.DAT from the specified game directory. Note that SC55.DAT is only
present if the game was installed or configured with Roland Sound Canvas (SC-55)
selected in the game setup. The file contains a sequence of 16-bit header sizes
followed by XOR-encrypted MIDI data streams (XOR key: 0x6B). The script
auto-detects the total song count by matching header size totals with the overall
file length. Each encrypted MIDI block is decrypted using XOR 0x6B and saved as
a Standard MIDI Format (.MID) file.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Fox Ranger (1992) [GS]"

# Song index to output filename lookup table.
# Add entries here if specific track titles are identified (e.g., 0: "01_TITLE").
LOOKUP_TABLE = {}

XOR_KEY = 0x6B


def extract_songs(input_dir, output_dir):
    """Extract MIDI tracks from SC55.DAT using LOOKUP_TABLE filenames."""
    if input_dir.is_file():
        dat_path = input_dir
    else:
        dat_path = input_dir / "SC55.DAT"
        if not dat_path.exists():
            if input_dir.exists() and input_dir.is_dir():
                for child in input_dir.iterdir():
                    if child.name.upper() == "SC55.DAT":
                        dat_path = child
                        break

    if not dat_path.exists():
        raise FileNotFoundError(
            f"SC55.DAT not found in {input_dir}. "
            "Please make sure the game was installed/configured with Roland SC-55 sound option."
        )

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    data = dat_path.read_bytes()
    total_size = len(data)
    song_count = None

    # Auto-detect song count by matching header sizes with total file length
    for count in range(1, 100):
        header_len = count * 2
        if header_len > total_size:
            break
        sizes = struct.unpack(f"<{count}H", data[:header_len])
        if header_len + sum(sizes) == total_size:
            song_count = count
            break

    if song_count is None:
        raise ValueError("Failed to auto-detect song count in SC55.DAT")

    print(f"Detected {song_count} songs in '{dat_path.name}'. Extracting...")

    file_pos = song_count * 2
    toc_pos = 0
    used_filenames = set()

    for number in range(song_count):
        file_size = struct.unpack_from("<H", data, toc_pos)[0]
        toc_pos += 2

        encrypted = data[file_pos : file_pos + file_size]
        decrypted = bytes(b ^ XOR_KEY for b in encrypted)

        # Determine output filename using LOOKUP_TABLE or fallback format
        if number in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[number]
        else:
            base_name = f"SC55_{number:02d}"

        if not base_name.lower().endswith(".mid"):
            filename = f"{base_name}.MID"
        else:
            filename = base_name

        # Resolve filename collisions
        if filename in used_filenames:
            stem = Path(filename).stem
            ext = Path(filename).suffix
            count_suffix = 2
            while f"{stem}_{count_suffix}{ext}" in used_filenames:
                count_suffix += 1
            filename = f"{stem}_{count_suffix}{ext}"

        used_filenames.add(filename)

        out_path = target_dir / filename
        out_path.write_bytes(decrypted)
        print(
            f"Extracted: {filename} (Offset: 0x{file_pos:06X}, Size: {file_size} bytes)"
        )

        file_pos += file_size

    print("Done!")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing SC55.DAT (default: current directory)",
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