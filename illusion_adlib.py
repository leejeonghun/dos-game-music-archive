#!/usr/bin/env python3
'''
Illusion Blaze

Extracts AdLib (FM) IMS BGM tracks and per-track BNK bank files from Illusion Blaze (1994).

Unpacking and File Conversion Structure:
Reads IFSM.IDT and IFSM.FS2 from the specified directory. IFSM.IDT contains multiple IMS
tracks concatenated back-to-back without a header table. Each IMS track ends with an
instrument table identified by the signature 'ww' (0x77 0x77). The script scans IFSM.IDT
to split each IMS track. Then, using IFSM.FS2 as the master instrument bank, it generates
a corresponding trimmed AdLib BNK bank file for each extracted IMS track containing only
the required instruments.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Illusion Blaze (1994) [FM]"

LOOKUP_TABLE = {}


def find_ims_segment_end(data: bytes, start: int):
    """Find the end offset of the first valid IMS instrument table signature ('ww') from start."""
    sig = b"ww"
    pos = start

    while True:
        pos = data.find(sig, pos)
        if pos == -1:
            return None

        if pos + 4 <= len(data):
            (num_instruments,) = struct.unpack("<H", data[pos + 2 : pos + 4])

            if 0 <= num_instruments <= 256:
                name_area_start = pos + 4
                name_area_end = name_area_start + (num_instruments * 9)

                if name_area_end <= len(data):
                    name_area = data[name_area_start:name_area_end]

                    if all(b == 0 or (32 <= b <= 126) for b in name_area):
                        instruments = []
                        offset = name_area_start
                        valid = True

                        for _ in range(num_instruments):
                            name_bytes = data[offset : offset + 9]
                            null_idx = name_bytes.find(b"\x00")
                            trimmed = name_bytes[: null_idx if null_idx != -1 else 9]
                            try:
                                inst_name = trimmed.decode("ascii").strip()
                            except UnicodeDecodeError:
                                valid = False
                                break
                            instruments.append(inst_name)
                            offset += 9

                        if valid:
                            return name_area_end, num_instruments, instruments

        pos += 1


def unpack_ims_container(data: bytes):
    """Scan container data and split concatenated IMS segments."""
    size = len(data)
    segments = []
    start = 0

    while start < size:
        result = find_ims_segment_end(data, start)
        if result is None:
            break

        end, num_instruments, instruments = result
        segments.append(
            {
                "start": start,
                "end": end,
                "data": data[start:end],
                "num_instruments": num_instruments,
                "instruments": instruments,
            }
        )
        start = end

    leftover = size - start
    return segments, leftover


def parse_bnk_file(data: bytes):
    """Parse AdLib BNK bank file format."""
    try:
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
            "record_size": record_size,
            "num_instruments": num_instruments,
            "num_used": num_used,
            "entries": entries,
        }
    except Exception:
        return None


def build_subset_bnk(bnk_info: dict, instrument_names: list):
    """Build a subset BNK binary containing only the requested instruments."""
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

    assert record_size * num == len(data_section)

    return bytes(header) + bytes(name_table) + bytes(data_section), matched, missing


def extract_ims(input_dir: Path, output_dir: Path):
    """Extract IMS tracks from IFSM.IDT and create matching BNK files from IFSM.FS2 using LOOKUP_TABLE."""
    if input_dir.is_file():
        idt_path = input_dir
        input_dir = input_dir.parent
    else:
        idt_path = input_dir / "IFSM.IDT"
        if not idt_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "IFSM.IDT":
                    idt_path = child
                    break

    if not idt_path.exists():
        raise FileNotFoundError(f"IFSM.IDT not found in {input_dir}")

    bank_path = input_dir / "IFSM.FS2"
    if not bank_path.exists():
        for child in input_dir.iterdir():
            if child.name.upper() == "IFSM.FS2":
                bank_path = child
                break

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    idt_data = idt_path.read_bytes()
    segments, leftover = unpack_ims_container(idt_data)

    if not segments:
        raise ValueError(f"No valid IMS tracks found in {idt_path.name}")

    bnk_info = None
    if bank_path and bank_path.exists():
        bank_data = bank_path.read_bytes()
        bnk_info = parse_bnk_file(bank_data)

    used_filenames = set()

    for idx, seg in enumerate(segments):
        if idx in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[idx]
        else:
            base_name = f"{idt_path.stem}_{idx:02d}"

        if base_name.upper().endswith(".IMS"):
            stem = Path(base_name).stem
        else:
            stem = base_name

        ims_filename = f"{stem}.IMS"
        bnk_filename = f"{stem}.BNK"

        if ims_filename in used_filenames:
            count = 2
            while f"{stem}_{count}.IMS" in used_filenames:
                count += 1
            stem = f"{stem}_{count}"
            ims_filename = f"{stem}.IMS"
            bnk_filename = f"{stem}.BNK"

        used_filenames.add(ims_filename)

        ims_out_path = target_dir / ims_filename
        ims_out_path.write_bytes(seg["data"])

        size_kb = len(seg["data"]) / 1024
        log_line = f"{ims_filename}: {size_kb:.2f} KB, {seg['num_instruments']} instruments"

        if bnk_info:
            inst_names = [n.strip() for n in seg["instruments"] if n.strip()]
            subset_bytes, matched, missing = build_subset_bnk(bnk_info, inst_names)
            if subset_bytes:
                bnk_out_path = target_dir / bnk_filename
                bnk_out_path.write_bytes(subset_bytes)
                log_line += f" | BNK created ({len(matched)}/{len(inst_names)} instruments)"
            else:
                log_line += " | BNK creation failed"

        print(log_line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing IFSM.IDT (default: current directory)",
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