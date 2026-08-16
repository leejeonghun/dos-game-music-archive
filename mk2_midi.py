#!/usr/bin/env python3
'''
Mortal Kombat II (MS-DOS)

Extracts and converts General MIDI (GM) BGM tracks from Mortal Kombat II (1995).

Unpacking and File Conversion Structure:
Mortal Kombat II (DOS) uses the Sound Images Generation 2 sound driver (SCC.BIN).
The music files (*.MID) are packaged in a proprietary Sound Images binary format
rather than standard MIDI files.

File Structure and Conversion Pipeline:
1. Header and Footer Resolution:
   - Byte 0: Magic byte (0x01).
   - Bytes 1-2: 16-bit little-endian pointer to the footer offset table pointer.
   - Footer contains:
     * Ticks per quarter note (Division, 1 byte)
     * Tempo in BPM (1 byte)
     * Track count (1 byte)
     * Track Table of Contents (TOC): 16-bit little-endian offsets for each track.
2. Track Event Parsing (Derived from SCC.BIN disassembly):
   - Delta times are encoded as standard 7-bit Variable-Length Quantities (VLQ).
   - Events are interpreted based on SCC.BIN command dispatch table:
     * 0x00..0x7F: Note On (Note, Velocity) -> MIDI 0x9n
     * 0x80..0x8F: Channel Select -> MIDI Channel Prefix meta event (FF 20 01)
     * 0x90: Note Off (specified note) -> MIDI 0x9n with velocity 0
     * 0x91: Track End
     * 0x92: Program Change -> MIDI 0xCn
     * 0x93: Internal Parameter (1 byte operand, skipped, delta accumulated)
     * 0x94: No-op (0 operands)
     * 0x95: Pitch Bend (1 byte MSB) -> MIDI 0xEn
     * 0x96: Volume (CC#7) -> MIDI 0xBn 0x07
     * 0x97: Internal Driver Storage (1 byte operand, skipped, delta accumulated)
     * 0x98: Loop Jump -> Emits CC#111=1 marker and terminates track
     * 0x99: Note Off (last active note) -> MIDI 0x9n with velocity 0
     * 0x9A: Internal Address Shift (0 operands)
     * 0x9B: Pan (CC#10) -> MIDI 0xBn 0x0A
     * 0x9C: Loop Point Marker -> Emits CC#111=0 marker
     * 0x9D: Generic Controller -> MIDI 0xBn (CC#, Value)
3. Standard MIDI File (SMF) Assembly:
   - Header chunk (MThd): Format 1, Track Count, Division.
   - Track 0 Tempo Meta Event: 0xFF 0x51 0x03 calculated from BPM.
   - Track Chunks (MTrk): Delta-time normalized events ending with End of Track (FF 2F 00).
'''

import argparse
import struct
from pathlib import Path

FOLDER_NAME = "Mortal Kombat II (1995) [GM]"

LOOKUP_TABLE = {}


class ConversionError(Exception):
    """Raised when Sound Images conversion fails."""
    pass


def flush_vlq(value: int) -> bytes:
    """Encode an integer value into MIDI Variable-Length Quantity (VLQ) bytes."""
    buf = bytearray()
    v = value & 0x0FFFFFFF
    chunk = [v & 0x7F]
    v >>= 7
    while v:
        chunk.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.extend(reversed(chunk))
    return bytes(buf)


def convert_sound_images_to_smf(in_data: bytes) -> bytes:
    """Convert Sound Images Generation 2 binary data to Standard MIDI (Format 1)."""
    in_size = len(in_data)
    if in_size < 3:
        raise ConversionError("File too small.")

    if in_data.startswith(b"MThd"):
        raise ConversionError("Already a standard MIDI (SMF) file.")

    if in_data[0] != 0x01:
        raise ConversionError(f"Invalid magic header byte: 0x{in_data[0]:02X}")

    in_size = min(in_size, 0x10000)

    footer_ptr_ptr = struct.unpack_from("<H", in_data, 1)[0]
    if footer_ptr_ptr + 2 > in_size:
        raise ConversionError("Invalid footer pointer address.")

    footer_pos = struct.unpack_from("<H", in_data, footer_ptr_ptr)[0]
    if footer_pos + 3 > in_size:
        raise ConversionError("Invalid footer offset.")

    ticks_per_quarter = in_data[footer_pos + 0]
    tempo = in_data[footer_pos + 1]
    trk_count = in_data[footer_pos + 2]

    if tempo == 0:
        raise ConversionError("Tempo value is zero.")

    in_pos = footer_pos + 3
    if in_pos + trk_count * 2 > in_size:
        raise ConversionError("Corrupted track offset table.")

    trk_toc = []
    for _ in range(trk_count):
        trk_offset = struct.unpack_from("<H", in_data, in_pos)[0]
        trk_toc.append(trk_offset)
        in_pos += 2

    out = bytearray()
    out.extend(b"MThd")
    out.extend(struct.pack(">IHHH", 6, 1, trk_count, ticks_per_quarter))

    for cur_trk in range(trk_count):
        trk_out = bytearray()

        if cur_trk == 0:
            tempo_val = (1000000 * 60) // tempo
            trk_out.append(0x00)
            trk_out.extend(b"\xFF\x51\x03")
            trk_out.append((tempo_val >> 16) & 0xFF)
            trk_out.append((tempo_val >> 8) & 0xFF)
            trk_out.append(tempo_val & 0xFF)

        in_pos = trk_toc[cur_trk]
        trk_end = False
        last_note = 0x00
        sel_chn = 0
        pending_delta = 0

        while not trk_end:
            if in_pos >= in_size:
                break

            delta_start = in_pos
            while in_data[in_pos] & 0x80:
                in_pos += 1
                if in_pos >= in_size:
                    break
            if in_pos >= in_size:
                break
            in_pos += 1
            delta_bytes = in_data[delta_start:in_pos]

            delta_val = 0
            for b in delta_bytes:
                delta_val = (delta_val << 7) | (b & 0x7F)
            pending_delta += delta_val

            if in_pos >= in_size:
                break

            cmd = in_data[in_pos]
            event_bytes = None

            if not (cmd & 0x80):
                last_note = cmd
                vel = in_data[in_pos + 1] if in_pos + 1 < in_size else 64
                event_bytes = bytes([0x90 | sel_chn, last_note, vel])
                in_pos += 2

            elif (cmd & 0xF0) == 0x80:
                sel_chn = cmd & 0x0F
                event_bytes = bytes([0xFF, 0x20, 0x01, sel_chn])
                in_pos += 1

            elif cmd == 0x90:
                note = in_data[in_pos + 1] if in_pos + 1 < in_size else 0
                event_bytes = bytes([0x90 | sel_chn, note, 0x00])
                in_pos += 2

            elif cmd == 0x91:
                in_pos += 1
                trk_end = True

            elif cmd == 0x92:
                prog = in_data[in_pos + 1] if in_pos + 1 < in_size else 0
                event_bytes = bytes([0xC0 | sel_chn, prog])
                in_pos += 2

            elif cmd == 0x93:
                in_pos += 2

            elif cmd == 0x94:
                in_pos += 1

            elif cmd == 0x95:
                pb = in_data[in_pos + 1] if in_pos + 1 < in_size else 0
                event_bytes = bytes([0xE0 | sel_chn, 0x00, pb])
                in_pos += 2

            elif cmd == 0x96:
                vol = in_data[in_pos + 1] if in_pos + 1 < in_size else 0
                event_bytes = bytes([0xB0 | sel_chn, 0x07, vol])
                in_pos += 2

            elif cmd == 0x97:
                in_pos += 2

            elif cmd == 0x98:
                event_bytes = bytes([0xB0 | sel_chn, 0x6F, 0x01])
                in_pos += 1
                trk_end = True

            elif cmd == 0x99:
                note = last_note if last_note != 0x00 else 0
                event_bytes = bytes([0x90 | sel_chn, note, 0x00])
                in_pos += 1

            elif cmd == 0x9A:
                in_pos += 1

            elif cmd == 0x9B:
                pan = in_data[in_pos + 1] if in_pos + 1 < in_size else 0
                event_bytes = bytes([0xB0 | sel_chn, 0x0A, pan])
                in_pos += 2

            elif cmd == 0x9C:
                event_bytes = bytes([0xB0 | sel_chn, 0x6F, 0x00])
                in_pos += 1

            elif cmd == 0x9D:
                cc_num = in_data[in_pos + 1] if in_pos + 1 < in_size else 0
                cc_val = in_data[in_pos + 2] if in_pos + 2 < in_size else 0
                event_bytes = bytes([0xB0 | sel_chn, cc_num, cc_val])
                in_pos += 3

            else:
                in_pos += 1
                trk_end = True

            if event_bytes is None:
                continue

            trk_out.extend(flush_vlq(pending_delta))
            trk_out.extend(event_bytes)
            pending_delta = 0

        trk_out.extend(flush_vlq(pending_delta))
        trk_out.extend(b"\xFF\x2F\x00")

        out.extend(b"MTrk")
        out.extend(struct.pack(">I", len(trk_out)))
        out.extend(trk_out)

    return bytes(out)


def extract_songs(input_dir, output_dir):
    """Extract and convert MIDI files from input directory (recursively) using LOOKUP_TABLE."""
    input_path = Path(input_dir).resolve()
    output_path = Path(output_dir).resolve()

    target_dir = output_path / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)
    target_dir_resolved = target_dir.resolve()

    used_filenames = set()
    files_to_process = []

    # Recursively find all files in the input directory, excluding the output folder
    all_files = [
        p for p in input_path.rglob("*")
        if p.is_file() and target_dir_resolved not in p.resolve().parents and p.resolve() != target_dir_resolved
    ]

    if LOOKUP_TABLE:
        file_map = {}
        for f in all_files:
            file_map.setdefault(f.name.upper(), f)
            try:
                rel_str = str(f.relative_to(input_path)).replace("\\", "/").upper()
                file_map.setdefault(rel_str, f)
            except ValueError:
                pass

        for orig_key, new_name in LOOKUP_TABLE.items():
            key_str = str(orig_key).replace("\\", "/").upper()
            matched = file_map.get(key_str) or file_map.get(Path(key_str).name)
            if matched and matched not in [fp for fp, _ in files_to_process]:
                files_to_process.append((matched, new_name))
    else:
        for child in sorted(all_files):
            if child.suffix.lower() == ".mid":
                files_to_process.append((child, child.stem))

    for file_path, base_name in files_to_process:
        try:
            raw_data = file_path.read_bytes()
            midi_data = convert_sound_images_to_smf(raw_data)
        except Exception:
            # Skip invalid files or non-Sound Images format files
            continue

        if not str(base_name).lower().endswith(".mid"):
            filename = f"{base_name}.MID"
        else:
            filename = str(base_name)

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
        help="Input directory containing game files (default: current directory)",
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