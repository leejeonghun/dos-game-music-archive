#!/usr/bin/env python3
'''
Fox Ranger II: Second Mission

Extracts Roland GS MIDI BGM tracks from Fox Ranger II (1993).

Unpacking and File Conversion Structure:
Reads encrypted DAT files specified in the target list from the input directory
(game installation directory configured for Roland Sound Canvas SC-55 option).
Each DAT file is encrypted using XOR byte-wise encryption with key 0x4C.
After decryption, Standard MIDI Format (SMF) tracks starting with the 'MThd'
header are parsed and extracted into individual Standard MIDI (.MID) files.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Fox Ranger II (1993) [GS]"

LOOKUP_TABLE = {
    "BONUS.DAT": "BONUS",
    "BOSS.DAT": "BOSS",
    "CLEAR.DAT": "CLEAR",
    "E1.DAT": "E1",
    "E2.DAT": "E2",
    "GAMEOVER.DAT": "GAMEOVER",
    "P0.DAT": "P0",
    "P1.DAT": "P1",
    "P2.DAT": "P2",
    "P3.DAT": "P3",
    "SCORE.DAT": "SCORE",
    "STAGE1.DAT": "STAGE1",
    "STAGE2.DAT": "STAGE2",
    "STAGE3.DAT": "STAGE3",
    "STAGE4.DAT": "STAGE4",
    "STAGE5.DAT": "STAGE5",
    "STAGE6.DAT": "STAGE6",
    "TALK.DAT": "TALK",
}

XOR_KEY = 0x4C


def decrypt_data(data, key=XOR_KEY):
    """Decrypt data using XOR cipher."""
    return bytes([b ^ key for b in data])


def parse_smf_length(data, pos):
    """Calculate exact byte length of SMF starting at pos."""
    if len(data) - pos < 14:
        return None
    if data[pos:pos + 4] != b"MThd":
        return None
    header_len = struct.unpack_from(">I", data, pos + 4)[0]
    num_tracks = struct.unpack_from(">H", data, pos + 10)[0]
    curr = pos + 8 + header_len
    for _ in range(num_tracks):
        if curr + 8 > len(data):
            return None
        if data[curr:curr + 4] != b"MTrk":
            return None
        trk_len = struct.unpack_from(">I", data, curr + 4)[0]
        curr += 8 + trk_len
    return curr - pos


def extract_midi(data):
    """Find and extract all SMF MIDI blocks from decrypted data."""
    midi_files = []
    indices = []
    idx = data.find(b"MThd")
    while idx != -1:
        indices.append(idx)
        idx = data.find(b"MThd", idx + 4)

    for i in range(len(indices)):
        start = indices[i]
        smf_len = parse_smf_length(data, start)
        if smf_len is not None:
            midi_files.append(data[start:start + smf_len])
        else:
            end = indices[i + 1] if i + 1 < len(indices) else len(data)
            midi_files.append(data[start:end])

    return midi_files


def extract_songs(input_dir, output_dir):
    """Extract MIDI files from target DAT files using LOOKUP_TABLE filenames."""
    input_path = Path(input_dir)
    output_path = Path(output_dir)

    target_dir = output_path / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    used_filenames = set()

    for dat_filename, base_name in LOOKUP_TABLE.items():
        file_path = input_path / dat_filename
        if not file_path.exists():
            for child in input_path.iterdir():
                if child.name.upper() == dat_filename.upper():
                    file_path = child
                    break

        if not file_path.exists():
            continue

        raw_data = file_path.read_bytes()
        decrypted_data = decrypt_data(raw_data)
        midi_files = extract_midi(decrypted_data)

        for i, midi_data in enumerate(midi_files):
            if len(midi_files) == 1:
                name = base_name
            else:
                name = f"{base_name}_song_{i}"

            if not name.lower().endswith(".mid"):
                filename = f"{name}.MID"
            else:
                filename = name

            if filename in used_filenames:
                stem = Path(filename).stem
                ext = Path(filename).suffix
                count = 2
                while f"{stem}_{count}{ext}" in used_filenames:
                    count += 1
                filename = f"{stem}_{count}{ext}"

            used_filenames.add(filename)

            out_path = target_dir / filename
            out_path.write_bytes(midi_data)
            print(f"{filename}: {len(midi_data)} MIDI bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing target DAT files (default: current directory)",
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