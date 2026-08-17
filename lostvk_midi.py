#!/usr/bin/env python3
'''
The Lost Vikings

Extracts General MIDI (GM) BGM tracks from The Lost Vikings (1993).

Unpacking and File Conversion Structure:
Reads DATA.DAT from the specified directory. DATA.DAT begins with a 32-bit
little-endian offset table defining the boundaries and sizes of packed resource
entries. The General MIDI BGM entries are extracted and decompressed using the
game's 4096-byte sliding window LZSS algorithm.
Each decompressed stream is in Miles Sound System XMIDI format (IFF FORM XMID /
CAT XMID). The converter parses the EVNT chunks, converts XMIDI run-length
delta times and duration-based note-on events into standard MIDI running delta
times with scheduled note-off events, strips proprietary driver controller
messages (CC 110-120), and writes Standard MIDI Format (SMF Type 0) files.
'''

import argparse
import struct
from bisect import insort
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

FOLDER_NAME = "The Lost Vikings (1993) [GM]"

LOOKUP_TABLE = {}

GM_MUSIC_INDEXES = [498, 493, 468, 473, 478, 483, 503, 488, 508, 513]

_DEFAULT_TEMPO = 120
_XMI_FREQ = 120
_DEFAULT_TIMEBASE = _XMI_FREQ * 60 // _DEFAULT_TEMPO  # 60
_DEFAULT_QUARTER_NOTE_MICROS = 60 * 1_000_000 // _DEFAULT_TEMPO  # 500000
_MIDI_TIMEBASE = 960
_TRACK_LENGTH_OFFSET = 18
_TRACK_DATA_OFFSET = 22
_MAX_NOTE_OFFS = 1000

# Miles Sound System reserved controller range (110-120)
_XMIDI_RESERVED_CC_LOW = 0x6E   # 110
_XMIDI_RESERVED_CC_HIGH = 0x78  # 120


# ===========================================================================
# XMI to MIDI Converter
# ===========================================================================

class XmiError(Exception):
    """Raised when an error occurs during XMI parsing or conversion."""
    pass


@dataclass
class SequenceInfo:
    index: int = 0
    form_offset: int = 0
    form_size: int = 0
    event_offset: int = 0
    event_size: int = 0
    has_timb: bool = False
    has_rbrn: bool = False


@dataclass(order=True)
class _NoteOffEvent:
    deadline: int
    status: int = field(compare=False, default=0)
    note: int = field(compare=False, default=0)


def _need(cursor: int, end: int, count: int, context: str):
    if cursor > end or count > (end - cursor):
        raise XmiError(f"Invalid XMI: truncated {context} data.")


def _has_tag(data: bytes, cursor: int, end: int, tag: bytes) -> bool:
    n = len(tag)
    return cursor <= end and (end - cursor) >= n and data[cursor:cursor + n] == tag


def _read_be32(data: bytes, cursor: int, end: int):
    _need(cursor, end, 4, "32-bit integer")
    value = struct.unpack_from(">I", data, cursor)[0]
    return value, cursor + 4


def _chunk_payload_end(data: bytes, payload: int, limit: int, length: int, context: str) -> int:
    if payload > limit:
        raise XmiError(f"Invalid XMI: truncated {context} data.")
    return payload + min(length, limit - payload)


def _next_chunk(payload_end: int, limit: int, length: int) -> int:
    nxt = payload_end
    if (length & 1) != 0 and nxt < limit:
        nxt += 1
    return nxt


def _scan_form_xmid(data: bytes, chunk_start: int, payload: int, chunk_end: int,
                     length: int, sequences: List[SequenceInfo]):
    if length < 4:
        raise XmiError("Invalid XMI: FORM chunk is too small.")
    if not _has_tag(data, payload, chunk_end, b"XMID"):
        return

    info = SequenceInfo(index=len(sequences))
    info.form_offset = chunk_start
    info.form_size = 8 + length

    local = payload + 4
    while local < chunk_end:
        if chunk_end - local < 8:
            break
        is_timb = _has_tag(data, local, chunk_end, b"TIMB")
        is_rbrn = _has_tag(data, local, chunk_end, b"RBRN")
        is_evnt = _has_tag(data, local, chunk_end, b"EVNT")
        local += 4
        local_length, local = _read_be32(data, local, chunk_end)
        local_payload = local
        local_end = _chunk_payload_end(data, local_payload, chunk_end, local_length, "sequence chunk")

        if is_timb:
            info.has_timb = True
        elif is_rbrn:
            info.has_rbrn = True
        elif is_evnt:
            info.event_offset = local_payload
            info.event_size = local_end - local_payload

        local = _next_chunk(local_end, chunk_end, local_length)

    if info.event_size == 0:
        raise XmiError("Invalid XMI: FORM XMID contains no EVNT chunk.")

    sequences.append(info)


def _scan_catalog_xmid(data: bytes, payload: int, chunk_end: int, length: int,
                        sequences: List[SequenceInfo]):
    if length < 4:
        raise XmiError("Invalid XMI: CAT chunk is too small.")
    if not _has_tag(data, payload, chunk_end, b"XMID"):
        return

    child = payload + 4
    while child < chunk_end:
        if chunk_end - child < 8:
            break
        child_start = child
        is_form = _has_tag(data, child, chunk_end, b"FORM")
        child += 4
        child_length, child = _read_be32(data, child, chunk_end)
        child_payload = child
        child_end = _chunk_payload_end(data, child_payload, chunk_end, child_length, "CAT sub-chunk")

        if is_form:
            _scan_form_xmid(data, child_start, child_payload, child_end, child_length, sequences)

        child = _next_chunk(child_end, chunk_end, child_length)


def sequence_infos(data: bytes) -> List[SequenceInfo]:
    if not data:
        raise XmiError("Invalid XMI: empty data.")

    sequences: List[SequenceInfo] = []
    root = 0
    end = len(data)

    while root < end:
        if end - root < 8:
            break
        root_start = root
        is_form = _has_tag(data, root, end, b"FORM")
        is_catalog = _has_tag(data, root, end, b"CAT ")
        root += 4
        root_length, root = _read_be32(data, root, end)
        root_payload = root
        root_end = _chunk_payload_end(data, root_payload, end, root_length, "top-level IFF chunk")

        if is_form:
            _scan_form_xmid(data, root_start, root_payload, root_end, root_length, sequences)
        elif is_catalog:
            _scan_catalog_xmid(data, root_payload, root_end, root_length, sequences)

        root = _next_chunk(root_end, end, root_length)

    if not sequences:
        raise XmiError("Invalid XMI: no FORM XMID sequence found.")

    return sequences


def _append_be32(buf: bytearray, value: int):
    buf += struct.pack(">I", value & 0xFFFFFFFF)


def _patch_be32(buf: bytearray, offset: int, value: int):
    buf[offset:offset + 4] = struct.pack(">I", value & 0xFFFFFFFF)


def _read_varlen(data: bytes, cursor: int, end: int):
    value = 0
    for _ in range(5):
        _need(cursor, end, 1, "variable-length quantity")
        b = data[cursor]
        cursor += 1
        value = (value << 7) | (b & 0x7F)
        if (b & 0x80) == 0:
            return value, cursor
    raise XmiError("Invalid XMI: variable-length quantity exceeds limit.")


def _append_varlen(buf: bytearray, value: int):
    encoded = [value & 0x7F]
    value >>= 7
    while value:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    buf += bytes(reversed(encoded))


def _read_xmi_delta(data: bytes, cursor: int, end: int):
    delay = 0
    while cursor < end and data[cursor] < 0x80:
        delay += data[cursor]
        cursor += 1
    return delay, cursor


def _scale_delta(delta: int, quarter_note_micros: int) -> int:
    denominator = quarter_note_micros * _DEFAULT_TIMEBASE
    if denominator == 0:
        raise XmiError("Invalid MIDI tempo: quarter-note duration is zero.")
    numerator = delta * _MIDI_TIMEBASE * _DEFAULT_QUARTER_NOTE_MICROS
    return (numerator + denominator // 2) // denominator


def _channel_event_size(status: int) -> int:
    high = status & 0xF0
    if high in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
        return 3
    if high in (0xC0, 0xD0):
        return 2
    return 0


def convert_xmi_to_smf(data: bytes, sequence_index: int = 0) -> bytes:
    if not data:
        raise XmiError("Invalid XMI: empty data.")

    sequences = sequence_infos(data)
    if sequence_index >= len(sequences):
        raise XmiError(
            f"Invalid XMI: sequence index {sequence_index} out of range "
            f"({len(sequences)} sequences available)."
        )

    seq = sequences[sequence_index]
    cursor = seq.event_offset
    event_end = cursor + seq.event_size

    midi = bytearray()
    midi += b"MThd"
    _append_be32(midi, 6)
    midi += bytes([0, 0, 0, 1])
    midi += struct.pack(">H", _MIDI_TIMEBASE)
    midi += b"MTrk" + bytes([0, 0, 0, 0])

    note_offs: List[_NoteOffEvent] = []
    quarter_note_micros = _DEFAULT_QUARTER_NOTE_MICROS
    clock = 0
    flushed_clock = 0

    def flush_note_off(ev: _NoteOffEvent):
        nonlocal flushed_clock
        _append_varlen(midi, _scale_delta(ev.deadline - flushed_clock, quarter_note_micros))
        midi.append(ev.status & 0x8F)
        midi.append(ev.note)
        midi.append(0x7F)
        flushed_clock = ev.deadline

    def flush_due_note_offs(upto_clock: int):
        while note_offs and note_offs[0].deadline <= upto_clock:
            flush_note_off(note_offs.pop(0))

    def emit_event_delta():
        nonlocal flushed_clock
        _append_varlen(midi, _scale_delta(clock - flushed_clock, quarter_note_micros))
        flushed_clock = clock

    def queue_note_off(duration: int, status: int, note: int):
        if duration <= 0:
            duration = 1
        if len(note_offs) >= _MAX_NOTE_OFFS:
            raise XmiError("Too many pending Note-Off events.")
        insort(note_offs, _NoteOffEvent(clock + duration, status, note))

    ended_properly = False

    while cursor < event_end:
        if data[cursor] < 0x80:
            delay, cursor = _read_xmi_delta(data, cursor, event_end)
            clock += delay
            flush_due_note_offs(clock)
            continue

        status = data[cursor]

        if status == 0xFF:
            _need(cursor, event_end, 2, "meta event")
            meta_type = data[cursor + 1]

            if meta_type == 0x2F:
                cursor += 2
                if cursor >= event_end:
                    meta_length = 0
                else:
                    meta_length, cursor = _read_varlen(data, cursor, event_end)
                    _need(cursor, event_end, meta_length, "end-of-track payload")
                    cursor += meta_length

                flush_due_note_offs(clock)
                for ev in note_offs:
                    _append_varlen(midi, _scale_delta(clock - flushed_clock, quarter_note_micros))
                    midi.append(ev.status & 0x8F)
                    midi.append(ev.note)
                    midi.append(0x7F)
                    flushed_clock = clock
                note_offs.clear()

                emit_event_delta()
                midi += bytes([0xFF, 0x2F, 0x00])
                ended_properly = True
                break

            emit_event_delta()
            midi.append(data[cursor])
            cursor += 1
            midi.append(data[cursor])
            cursor += 1
            length_start = cursor
            meta_length, cursor = _read_varlen(data, cursor, event_end)
            midi += data[length_start:cursor]
            _need(cursor, event_end, meta_length, "meta payload")

            if meta_type == 0x51 and meta_length == 3:
                quarter_note_micros = (
                    (data[cursor] << 16) | (data[cursor + 1] << 8) | data[cursor + 2]
                )

            midi += data[cursor:cursor + meta_length]
            cursor += meta_length

        elif status in (0xF0, 0xF7):
            emit_event_delta()
            midi.append(data[cursor])
            cursor += 1
            length_start = cursor
            sysex_length, cursor = _read_varlen(data, cursor, event_end)
            midi += data[length_start:cursor]
            _need(cursor, event_end, sysex_length, "SysEx payload")
            midi += data[cursor:cursor + sysex_length]
            cursor += sysex_length

        else:
            event_size = _channel_event_size(status)
            if event_size == 0:
                cursor += 1
                continue

            event_status = data[cursor]
            event_note = data[cursor + 1] if event_size > 1 else 0
            _need(cursor, event_end, event_size, "event payload")

            is_reserved_xmidi_cc = (
                (event_status & 0xF0) == 0xB0
                and _XMIDI_RESERVED_CC_LOW <= event_note <= _XMIDI_RESERVED_CC_HIGH
            )

            if not is_reserved_xmidi_cc:
                emit_event_delta()
                midi += data[cursor:cursor + event_size]
            cursor += event_size

            if (event_status & 0xF0) == 0x90:
                duration, cursor = _read_varlen(data, cursor, event_end)
                queue_note_off(duration, event_status, event_note)

    if not ended_properly:
        flush_due_note_offs(clock)
        for ev in note_offs:
            _append_varlen(midi, _scale_delta(clock - flushed_clock, quarter_note_micros))
            midi.append(ev.status & 0x8F)
            midi.append(ev.note)
            midi.append(0x7F)
            flushed_clock = clock
        note_offs.clear()
        emit_event_delta()
        midi += bytes([0xFF, 0x2F, 0x00])

    track_length = len(midi) - _TRACK_DATA_OFFSET
    _patch_be32(midi, _TRACK_LENGTH_OFFSET, track_length)

    return bytes(midi)


def convert_all(data: bytes) -> List[bytes]:
    """Convert all sequences inside XMI payload to SMF Type 0 byte streams."""
    sequences = sequence_infos(data)
    return [convert_xmi_to_smf(data, seq.index) for seq in sequences]


# ===========================================================================
# DATA.DAT Decompression and Archive Parsing
# ===========================================================================

def lzss_decompress(src: bytes, decompressed_len: int) -> bytes:
    """Decompress Lost Vikings DATA.DAT LZSS stream."""
    window = bytearray(0x1000)
    win_pos = 0
    out = bytearray()

    pos = 0
    src_len = len(src)
    bitmap = 0
    bits_left = 0

    while len(out) < decompressed_len:
        if bits_left <= 0:
            if pos >= src_len:
                break
            bitmap = src[pos]
            pos += 1
            bits_left = 8

        flag = bitmap & 1
        bitmap >>= 1
        bits_left -= 1

        if flag == 1:
            if pos >= src_len:
                break
            b = src[pos]
            pos += 1
            out.append(b)
            window[win_pos] = b
            win_pos = (win_pos + 1) & 0x0FFF
        else:
            if pos + 1 >= src_len:
                break
            lo = src[pos]
            hi = src[pos + 1]
            pos += 2
            value = lo | (hi << 8)
            offset = value & 0x0FFF
            length = (value >> 12) + 3
            for _ in range(length):
                if len(out) >= decompressed_len:
                    break
                b = window[offset]
                offset = (offset + 1) & 0x0FFF
                out.append(b)
                window[win_pos] = b
                win_pos = (win_pos + 1) & 0x0FFF

    return bytes(out[:decompressed_len])


def parse_dat(data: bytes):
    """Parse DATA.DAT offset table and return (file_count, entries)."""
    if len(data) < 4:
        raise ValueError("DATA.DAT file is too short.")
    first_offset = struct.unpack_from("<I", data, 0)[0]
    file_count = first_offset // 4
    offsets = list(struct.unpack_from("<%dI" % file_count, data, 0))

    entries = []
    for i in range(file_count):
        off = offsets[i]
        if i + 1 < file_count:
            size = offsets[i + 1] - off
        else:
            size = len(data) - off
        entries.append((off, size))

    for i, (off, size) in enumerate(entries):
        if size < 0:
            raise ValueError(f"Entry {i}: negative size encountered.")
        if off + size > len(data):
            raise ValueError(f"Entry {i}: entry offset out of archive range.")

    return file_count, entries


def extract_entry(data: bytes, offset: int, size: int) -> bytes:
    """Decompress a single entry from DATA.DAT."""
    chunk = data[offset:offset + size]
    if len(chunk) < 2:
        return chunk

    decompressed_len = struct.unpack_from("<H", chunk, 0)[0]
    compressed_payload = chunk[2:]
    return lzss_decompress(compressed_payload, decompressed_len)


def is_xmi(payload: bytes) -> bool:
    """Check if payload is an IFF XMI format."""
    if len(payload) < 12:
        return False
    if payload[0:4] != b"FORM":
        return False
    formtype = payload[8:12]
    return formtype in (b"XDIR", b"XMID")


# ===========================================================================
# Extraction and Lookup Table Handling
# ===========================================================================

def extract_songs(input_dir: Path, output_dir: Path):
    """Extract GM tracks from DATA.DAT and save them as SMF Type 0 MIDI files."""
    if input_dir.is_file():
        dat_path = input_dir
    else:
        dat_path = input_dir / "DATA.DAT"
        if not dat_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "DATA.DAT":
                    dat_path = child
                    break

    if not dat_path.exists():
        raise FileNotFoundError(f"DATA.DAT not found in {input_dir}")

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    dat_data = dat_path.read_bytes()
    file_count, entries = parse_dat(dat_data)

    used_filenames = set()

    for order_idx, file_idx in enumerate(GM_MUSIC_INDEXES):
        if file_idx >= file_count:
            print(f"Warning: Entry #{file_idx:03d} exceeds archive count ({file_count}).")
            continue

        off, size = entries[file_idx]
        if size == 0:
            print(f"Warning: Entry #{file_idx:03d} has zero size.")
            continue

        try:
            payload = extract_entry(dat_data, off, size)
        except Exception as e:
            print(f"Failed to decompress entry #{file_idx:03d}: {e}")
            continue

        if not is_xmi(payload):
            print(f"Entry #{file_idx:03d} is not a valid XMI file.")
            continue

        try:
            midis = convert_all(payload)
        except XmiError as e:
            print(f"Failed to convert entry #{file_idx:03d} to MIDI: {e}")
            continue

        for seq_idx, midi_bytes in enumerate(midis):
            if order_idx in LOOKUP_TABLE:
                base_name = LOOKUP_TABLE[order_idx]
            elif str(order_idx) in LOOKUP_TABLE:
                base_name = LOOKUP_TABLE[str(order_idx)]
            elif file_idx in LOOKUP_TABLE:
                base_name = LOOKUP_TABLE[file_idx]
            else:
                base_name = f"GM_{order_idx:02d}"

            if len(midis) > 1:
                base_name = f"{base_name}_{seq_idx:02d}"

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
            out_path.write_bytes(midi_bytes)
            print(f"{filename}: {len(midi_bytes)} MIDI bytes (DAT entry #{file_idx:03d})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing DATA.DAT (default: current directory)",
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