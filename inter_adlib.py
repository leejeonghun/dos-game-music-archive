#!/usr/bin/env python3
'''
Interrupt (Iron Blood, 1994)

Extracts AdLib (FM) IMS music tracks and generates matching BNK instrument bank
files from Interrupt (Iron Blood, 1994) by Family Production.

Unpacking and File Conversion Structure:
Scans files in the specified directory for IMS audio binary structures by searching
for the 0x77 0x77 ("ww") table signature and instrument list. If SIGNAL.BNK is present
in the input path, a customized, track-specific subset BNK file is dynamically
generated for each extracted IMS file to support AdPlug and foobar2000 playback.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Interrupt (Iron Blood, 1994) [FM]"

LOOKUP_TABLE = {}


def parse_ims_file(file_path):
    """
    Parses IMS file format by locating 0x77 0x77 ('ww') signature
    and verifying instrument list table.
    """
    try:
        file_size = file_path.stat().st_size
        if file_size < 10 or file_size > 50 * 1024 * 1024:
            return False, None

        data = file_path.read_bytes()
        sig = b"ww"  # 0x77, 0x77
        pos = 0

        while True:
            pos = data.find(sig, pos)
            if pos == -1:
                break

            if pos + 4 <= file_size:
                (num_instruments,) = struct.unpack("<H", data[pos + 2 : pos + 4])

                if 0 <= num_instruments <= 256:
                    expected_end = pos + 4 + (num_instruments * 9)

                    if expected_end == file_size:
                        instruments = []
                        offset = pos + 4
                        valid_names = True

                        for _ in range(num_instruments):
                            name_bytes = data[offset : offset + 9]

                            if not all(
                                b == 0 or (32 <= b <= 126) for b in name_bytes
                            ):
                                valid_names = False
                                break

                            null_idx = name_bytes.find(b"\x00")
                            if null_idx != -1:
                                name_bytes = name_bytes[:null_idx]

                            inst_name = name_bytes.decode(
                                "ascii", errors="replace"
                            ).strip()
                            instruments.append(inst_name)
                            offset += 9

                        if valid_names:
                            return True, {
                                "num_instruments": num_instruments,
                                "instruments": instruments,
                                "table_offset": pos,
                            }

            pos += 1

        return False, None

    except Exception:
        return False, None


def parse_bnk_file(bnk_path):
    """
    Parses AdLib Instrument Bank (.BNK) file format according to ModdingWiki specs.
    """
    try:
        data = bnk_path.read_bytes()

        if len(data) < 28 or data[2:8] != b"ADLIB-":
            return None

        num_used, num_instruments = struct.unpack("<HH", data[8:12])
        offset_name, offset_data = struct.unpack("<II", data[12:20])

        if num_instruments == 0 or offset_data <= offset_name:
            return None

        names_table_size = num_instruments * 12
        if offset_name + names_table_size > len(data):
            return None
        if offset_data != offset_name + names_table_size:
            return None

        remaining = len(data) - offset_data
        if remaining <= 0 or remaining % num_instruments != 0:
            return None
        record_size = remaining // num_instruments

        entries = []
        pos = offset_name
        for _ in range(num_instruments):
            idx, flags = struct.unpack("<HB", data[pos : pos + 3])
            raw_name = data[pos + 3 : pos + 12]
            pos += 12

            null_idx = raw_name.find(b"\x00")
            name = raw_name[: null_idx if null_idx != -1 else 9].decode(
                "ascii", errors="replace"
            )

            rec_offset = offset_data + idx * record_size
            if rec_offset + record_size > len(data):
                continue
            record_bytes = data[rec_offset : rec_offset + record_size]

            entries.append((name, flags, record_bytes))

        return {
            "path": bnk_path,
            "record_size": record_size,
            "num_instruments": num_instruments,
            "num_used": num_used,
            "entries": entries,
        }
    except Exception:
        return None


def build_subset_bnk(bnk_info, instrument_names):
    """
    Builds a standalone subset BNK file containing only instruments used by a specific IMS file.
    """
    name_to_record = {}
    for name, flags, record_bytes in bnk_info["entries"]:
        if flags != 0 and name not in name_to_record:
            name_to_record[name] = record_bytes

    wanted = [n for n in dict.fromkeys(instrument_names) if n]
    matched = [n for n in wanted if n in name_to_record]
    missing = [n for n in wanted if n not in name_to_record]

    if not matched:
        return None, matched, missing

    matched_sorted = sorted(matched, key=lambda s: s.lower())
    record_size = bnk_info["record_size"]
    num = len(matched_sorted)

    header = struct.pack(
        "<BB6sHHII8s",
        1,
        0,
        b"ADLIB-",
        num,
        num,
        28,
        28 + num * 12,
        b"\x00" * 8,
    )

    name_table = bytearray()
    data_section = bytearray()
    for i, name in enumerate(matched_sorted):
        name_bytes = name.encode("ascii", errors="ignore")[:8]
        name_field = name_bytes + b"\x00" * (9 - len(name_bytes))
        name_table += struct.pack("<HB9s", i, 1, name_field)
        data_section += name_to_record[name]

    if record_size * num != len(data_section):
        return None, matched, missing

    return bytes(header) + bytes(name_table) + bytes(data_section), matched, missing


def find_signal_bnk(input_dir):
    """
    Searches specifically for SIGNAL.BNK file in the input directory.
    """
    if input_dir.is_file():
        input_dir = input_dir.parent
    for p in input_dir.rglob("*"):
        if p.is_file() and p.name.upper() == "SIGNAL.BNK":
            return p
    return None


def extract_ims(input_dir, output_dir):
    """
    Extracts IMS files and generates paired BNK files using SIGNAL.BNK.
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)

    target_dir = output_path / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    signal_bnk_path = find_signal_bnk(input_path)
    bnk_info = parse_bnk_file(signal_bnk_path) if signal_bnk_path else None

    if input_path.is_file():
        files_to_scan = [input_path]
    else:
        files_to_scan = sorted(
            [p for p in input_path.rglob("*") if p.is_file()],
            key=lambda x: str(x).lower(),
        )

    ims_files = []
    for path in files_to_scan:
        is_valid, info = parse_ims_file(path)
        if is_valid:
            ims_files.append((path, info))

    if not ims_files:
        raise FileNotFoundError(f"No valid IMS files found in {input_path}")

    used_filenames = set()

    for idx, (path, info) in enumerate(ims_files):
        stem = path.stem
        if idx in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[idx]
        elif stem in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[stem]
        elif stem.upper() in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[stem.upper()]
        else:
            base_name = stem

        if not base_name.upper().endswith(".IMS"):
            filename = f"{base_name}.IMS"
        else:
            filename = base_name

        if filename in used_filenames:
            f_stem = Path(filename).stem
            f_ext = Path(filename).suffix
            count = 2
            while f"{f_stem}_{count}{f_ext}" in used_filenames:
                count += 1
            filename = f"{f_stem}_{count}{f_ext}"

        used_filenames.add(filename)

        ims_out_path = target_dir / filename
        ims_out_path.write_bytes(path.read_bytes())

        bnk_status = "BNK skipped (SIGNAL.BNK missing)"
        if bnk_info:
            inst_names = [n.strip() for n in info["instruments"] if n.strip()]
            subset_bytes, matched, _ = build_subset_bnk(bnk_info, inst_names)

            if subset_bytes:
                bnk_filename = f"{Path(filename).stem}.BNK"
                bnk_out_path = target_dir / bnk_filename
                bnk_out_path.write_bytes(subset_bytes)
                bnk_status = f"BNK created ({len(matched)}/{len(inst_names)} instruments)"
            else:
                bnk_status = "BNK creation failed"

        print(
            f"{filename}: {path.stat().st_size / 1024:.2f} KB, "
            f"{info['num_instruments']} instruments | {bnk_status}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing Interrupt game data (default: current directory)",
    )
    parser.add_argument(
        "output_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Output directory (default: current directory)",
    )
    args = parser.parse_args()
    extract_ims(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()