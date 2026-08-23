#!/usr/bin/env python3
'''
The Day 5: Assault Dragon (1995)

Extracts Roland GS MIDI BGM tracks from The Day 5: Assault Dragon (1995).

Unpacking and File Conversion Structure:
Mirinae Software's .MUE files contain LZ-compressed Standard MIDI Files (SMF).
The compression format uses a bit-stream control word to distinguish literal
bytes, short back-references, and long back-references. After decompression,
System Exclusive (SysEx) initialization data from THEDAY5.SYX is prepended as
a dedicated track (MTrk) with delta-time 0 events, and the SMF header (MThd)
is converted to Format 1 with an incremented track count to accurately reproduce
the Roland GS sound module initialization.
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "The Day 5 - Assault Dragon (1995) [GS]"

LOOKUP_TABLE = {}


def mrndec_decompress(in_data: bytes) -> bytes:
    """Decompresses Mirinae Software LZ/RLE compressed stream (ported from mrndec.c)."""
    dec_buf = bytearray()
    in_pos = 0
    out_pos = 0
    in_size = len(in_data)

    def read_le16(pos):
        return in_data[pos] | (in_data[pos + 1] << 8)

    if in_size < 2:
        raise ValueError("Input file is too short.")

    ctrl_data = read_le16(in_pos)
    in_pos += 2
    ctrl_bits = 16

    while in_pos < in_size:
        # Bit 1: Literal byte flag
        carry = ctrl_data & 1
        ctrl_data >>= 1
        ctrl_bits -= 1
        if ctrl_bits == 0:
            ctrl_data = read_le16(in_pos)
            in_pos += 2
            ctrl_bits = 16
        if carry:
            dec_buf.append(in_data[in_pos])
            in_pos += 1
            out_pos += 1
            continue

        # Bit 2: Short vs Long back-reference
        carry = ctrl_data & 1
        ctrl_data >>= 1
        ctrl_bits -= 1
        if ctrl_bits == 0:
            ctrl_data = read_le16(in_pos)
            in_pos += 2
            ctrl_bits = 16

        if not carry:
            # Short back-reference
            carry3 = ctrl_data & 1
            ctrl_data >>= 1
            ctrl_bits -= 1
            if ctrl_bits == 0:
                ctrl_data = read_le16(in_pos)
                in_pos += 2
                ctrl_bits = 16
            copy_cnt = carry3

            carry4 = ctrl_data & 1
            ctrl_data >>= 1
            ctrl_bits -= 1
            if ctrl_bits == 0:
                ctrl_data = read_le16(in_pos)
                in_pos += 2
                ctrl_bits = 16
            copy_cnt = (copy_cnt << 1) | carry4
            copy_cnt += 2
            copy_ofs = -0x100 + in_data[in_pos]
            in_pos += 1
        else:
            # Long back-reference or special command
            ax = read_le16(in_pos)
            in_pos += 2
            copy_ofs = -0x2000 + (ax & 0x1FFF)
            copy_cnt = ax >> 13
            if copy_cnt != 0:
                copy_cnt += 2
            else:
                cmd = in_data[in_pos]
                in_pos += 1
                if cmd == 0:
                    # Segment reset
                    continue
                elif cmd == 1:
                    # End of file
                    break
                else:
                    copy_cnt = cmd + 1

        for _ in range(copy_cnt):
            dec_buf.append(dec_buf[out_pos + copy_ofs])
            out_pos += 1

    return bytes(dec_buf)


def write_vlq(value: int) -> bytes:
    """Encode an integer as a standard MIDI variable-length quantity (VLQ)."""
    if not 0 <= value <= 0x0FFFFFFF:
        raise ValueError(f"MIDI delta time out of range: {value}")
    buf = [value & 0x7F]
    value >>= 7
    while value > 0:
        buf.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(buf))


def split_sysex_messages(data: bytes):
    """Splits raw bytes into complete SysEx messages ending with 0xF7."""
    msgs = []
    cur = []
    for b in data:
        cur.append(b)
        if b == 0xF7:
            msgs.append(bytes(cur))
            cur = []
    return msgs


def build_sysex_init_track(sysex_data: bytes) -> bytes:
    """Builds an MTrk chunk containing delta-0 SysEx initialization messages."""
    events = bytearray()
    for msg in split_sysex_messages(sysex_data):
        if not msg or msg[0] != 0xF0:
            continue
        payload = msg[1:]  # Bytes following 0xF0 up to and including 0xF7
        events += write_vlq(0)          # Delta-time = 0
        events += bytes([0xF0])
        events += write_vlq(len(payload))
        events += payload

    # End of Track meta event
    events += write_vlq(0) + bytes([0xFF, 0x2F, 0x00])
    return b"MTrk" + struct.pack(">I", len(events)) + bytes(events)


def inject_sysex_track(smf: bytes, sysex_track: bytes) -> bytes:
    """Prepends the SysEx init track to the SMF and updates the MThd header."""
    if len(smf) < 14 or not smf.startswith(b"MThd"):
        raise ValueError("Decompressed stream does not contain a valid MThd header.")

    hdr_len = struct.unpack(">I", smf[4:8])[0]
    fmt, ntrks, division = struct.unpack(">HHH", smf[8:8 + hdr_len])
    new_fmt = fmt if fmt != 0 else 1
    new_ntrks = ntrks + 1

    new_header = (
        b"MThd"
        + struct.pack(">I", hdr_len)
        + struct.pack(">HHH", new_fmt, new_ntrks, division)
    )
    rest = smf[8 + hdr_len:]
    return new_header + sysex_track + rest


def extract_songs(input_dir: Path, output_dir: Path):
    """Decompresses .MUE files, injects THEDAY5.SYX, and exports renamed .MID files."""
    if not input_dir.exists():
        raise FileNotFoundError(f"Input path does not exist: {input_dir}")

    # Locate THEDAY5.SYX (case-insensitive)
    syx_path = None
    if input_dir.is_file():
        search_dir = input_dir.parent
    else:
        search_dir = input_dir

    for child in search_dir.iterdir():
        if child.is_file() and child.name.upper() == "THEDAY5.SYX":
            syx_path = child
            break

    if not syx_path or not syx_path.exists():
        raise FileNotFoundError(f"THEDAY5.SYX not found in {search_dir}")

    sysex_track = build_sysex_init_track(syx_path.read_bytes())

    # Find all .MUE files not starting with "A-"
    mue_files = []
    if input_dir.is_file():
        if input_dir.suffix.upper() == ".MUE" and not input_dir.name.upper().startswith("A-"):
            mue_files.append(input_dir)
    else:
        for child in sorted(search_dir.iterdir()):
            if child.is_file() and child.suffix.upper() == ".MUE":
                if not child.name.upper().startswith("A-"):
                    mue_files.append(child)

    if not mue_files:
        print(f"No valid .MUE files found in {search_dir}")
        return

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    used_filenames = set()

    for idx, mue_file in enumerate(mue_files):
        compressed_data = mue_file.read_bytes()
        decompressed_smf = mrndec_decompress(compressed_data)
        final_smf = inject_sysex_track(decompressed_smf, sysex_track)

        # Lookup Table / Renaming resolution
        stem = mue_file.stem
        name = mue_file.name

        if name in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[name]
        elif stem in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[stem]
        elif idx in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[idx]
        else:
            base_name = stem

        if not base_name.upper().endswith(".MID"):
            filename = f"{base_name}.MID"
        else:
            filename = base_name

        # Prevent duplicate filenames
        if filename in used_filenames:
            stem_out = Path(filename).stem
            ext_out = Path(filename).suffix
            count = 2
            while f"{stem_out}_{count}{ext_out}" in used_filenames:
                count += 1
            filename = f"{stem_out}_{count}{ext_out}"

        used_filenames.add(filename)

        out_path = target_dir / filename
        out_path.write_bytes(final_smf)
        print(f"{mue_file.name} ({len(compressed_data)} bytes) -> {filename} ({len(final_smf)} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing .MUE and THEDAY5.SYX files (default: current directory)",
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