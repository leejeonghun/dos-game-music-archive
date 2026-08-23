#!/usr/bin/env python3
'''
Flame Dragon 2 (1995)

Extracts Roland MT-32 background music from Flame Dragon 2 (1995).

Unpacking and File Conversion Structure:
Reads FDMUS.DAT from the specified directory or file path. The file contains
concatenated Miles Sound System XMIDI (.XMI) streams identified by the
"FORM ... XDIR" signatures. Each XMIDI stream consists of IFF-style chunks
(CAT/FORM XMID, EVNT, and TIMB).

The conversion logic parses each sequence, translates XMIDI delta-time and note
duration pairs into Standard MIDI File (SMF Type 0) running delta-times and
note-off events. It injects Roland MT-32 initialization System Exclusive
messages (master reset, partial reserve, part assignments, and reverb settings)
matching the game's original MT32MPU.MDI driver behavior, maps Roland-exclusive
XMIDI controllers into MT-32 DT1 SysEx writes, and strips internal sequencer
controllers before writing standard .MID files.
'''

import argparse
import re
import struct
from bisect import insort
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

FOLDER_NAME = "Flame Dragon 2 (1995) [MT-32]"

LOOKUP_TABLE = {}

_DEFAULT_TEMPO = 120
_XMI_FREQ = 120
_DEFAULT_TIMEBASE = _XMI_FREQ * 60 // _DEFAULT_TEMPO  # 60
_DEFAULT_QUARTER_NOTE_MICROS = 60 * 1_000_000 // _DEFAULT_TEMPO  # 500000
_MIDI_TIMEBASE = 960
_TRACK_LENGTH_OFFSET = 18
_TRACK_DATA_OFFSET = 22
_MAX_NOTE_OFFS = 1000

# XMIDI controller numbers recognized by Miles/AIL drivers
RHYTHM_KEY_TIMB = 58
PATCH_REVERB = 59
PATCH_BENDER = 60
REVERB_MODE = 61
REVERB_TIME = 62
REVERB_LEVEL = 63

CHAN_LOCK = 110
CHAN_PROTECT = 111
VOICE_PROTECT = 112
TIMBRE_PROTECT = 113
PATCH_BANK_SEL = 114
INDIRECT_C_PFX = 115
FOR_LOOP = 116
NEXT_LOOP = 117
CLEAR_BEAT_BAR = 118
CALLBACK_TRIG = 119
SEQ_INDEX = 120

_ROLAND_EXCLUSIVE_CCS = {
    RHYTHM_KEY_TIMB,
    PATCH_REVERB,
    PATCH_BENDER,
    REVERB_MODE,
    REVERB_TIME,
    REVERB_LEVEL,
}
_GENERAL_XMIDI_CC_LOW = CHAN_LOCK
_GENERAL_XMIDI_CC_HIGH = SEQ_INDEX


# ===========================================================================
# Roland MT-32 System Exclusive helpers
# ===========================================================================

def _roland_checksum(body) -> int:
    """Roland DT1 checksum: (0x80 - (sum(body) & 0x7F)) & 0x7F."""
    total = sum(body) & 0x7F
    return (0x80 - total) & 0x7F


def _roland_sysex(addr, data) -> bytes:
    """Build a complete Roland DT1 write SysEx message."""
    body = list(addr) + list(data)
    checksum = _roland_checksum(body)
    return bytes([0xF0, 0x41, 0x10, 0x16, 0x12]) + bytes(addr) + bytes(data) + bytes([checksum, 0xF7])


def _add_sysex_addr(addend: int, msb: int, ksb: int, lsb: int):
    """Add a byte offset to a 21-bit Roland address (base-128)."""
    lo = lsb + addend
    mid = ksb
    hi = msb
    while lo >= 0x80:
        lo -= 0x80
        mid += 1
    while mid >= 0x80:
        mid -= 0x80
        hi += 1
    return hi, mid, lo


def _write_patch_sysex(patch: int, index: int, value: int) -> bytes:
    """Write to Patch Temp Area (base address 05 00 00)."""
    addend = (patch * 8) + index
    msb, ksb, lsb = _add_sysex_addr(addend, 5, 0, 0)
    return _roland_sysex((msb, ksb, lsb), (value,))


def _write_system_sysex(index: int, value: int) -> bytes:
    """Write to System Area (base address 10 00 00)."""
    return _roland_sysex((0x10, 0x00, index), (value,))


_MT32_RESET_ALL = _roland_sysex((0x7F, 0x00, 0x00), (1,))
_MT32_CHANNEL_ASSIGN = _roland_sysex((0x10, 0x00, 0x0D), (1, 2, 3, 4, 5, 6, 7, 8, 9))
_MT32_PARTIAL_RESERVE = _roland_sysex((0x10, 0x00, 0x04), (3, 4, 3, 4, 3, 4, 3, 4, 4))
_MT32_REVERB_DEFAULT = _roland_sysex((0x10, 0x00, 0x01), (0, 3, 2))

MT32_INIT_SYSEX = [
    _MT32_RESET_ALL,
    _MT32_CHANNEL_ASSIGN,
    _MT32_PARTIAL_RESERVE,
    _MT32_REVERB_DEFAULT,
]


# ===========================================================================
# XMI Structure Parsing
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


# ===========================================================================
# Conversion Logic (XMI -> SMF Type 0)
# ===========================================================================

def convert_xmi_to_mt32_smf(data: bytes, sequence_index: int = 0) -> bytes:
    if not data:
        raise XmiError("Invalid XMI: empty data.")

    sequences = sequence_infos(data)
    if sequence_index >= len(sequences):
        raise XmiError(f"Invalid sequence index {sequence_index}")

    seq = sequences[sequence_index]
    cursor = seq.event_offset
    event_end = cursor + seq.event_size

    midi = bytearray()
    midi += b"MThd"
    _append_be32(midi, 6)
    midi += bytes([0, 0, 0, 1])
    midi += struct.pack(">H", _MIDI_TIMEBASE)
    midi += b"MTrk" + bytes([0, 0, 0, 0])

    for sysex_msg in MT32_INIT_SYSEX:
        midi.append(0x00)
        midi.append(0xF0)
        _append_varlen(midi, len(sysex_msg) - 1)
        midi += sysex_msg[1:]

    note_offs: List[_NoteOffEvent] = []
    quarter_note_micros = _DEFAULT_QUARTER_NOTE_MICROS
    clock = 0
    flushed_clock = 0
    channel_program: List[int] = [-1] * 16

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

    def emit_sysex(sysex_msg: bytes):
        emit_event_delta()
        midi.append(0xF0)
        _append_varlen(midi, len(sysex_msg) - 1)
        midi += sysex_msg[1:]

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
            channel = event_status & 0x0F
            event_note = data[cursor + 1] if event_size > 1 else 0
            _need(cursor, event_end, event_size, "event payload")

            high = event_status & 0xF0

            if high == 0xC0:
                channel_program[channel] = event_note
                emit_event_delta()
                midi += data[cursor:cursor + event_size]
                cursor += event_size
                continue

            if high == 0xB0:
                controller = event_note
                value = data[cursor + 2]
                cursor += event_size

                if controller in _ROLAND_EXCLUSIVE_CCS:
                    prog = channel_program[channel]
                    if controller == PATCH_REVERB and prog != -1:
                        emit_sysex(_write_patch_sysex(prog, 6, value))
                    elif controller == PATCH_BENDER and prog != -1:
                        emit_sysex(_write_patch_sysex(prog, 4, value))
                    elif controller == REVERB_MODE:
                        emit_sysex(_write_system_sysex(1, value))
                    elif controller == REVERB_TIME:
                        emit_sysex(_write_system_sysex(2, value))
                    elif controller == REVERB_LEVEL:
                        emit_sysex(_write_system_sysex(3, value))
                    continue

                if _GENERAL_XMIDI_CC_LOW <= controller <= _GENERAL_XMIDI_CC_HIGH:
                    continue

                emit_event_delta()
                midi += bytes([event_status, controller, value])
                continue

            emit_event_delta()
            midi += data[cursor:cursor + event_size]
            cursor += event_size

            if high == 0x90:
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
    """Convert every sequence inside an XMIDI stream to SMF Type 0."""
    sequences = sequence_infos(data)
    return [convert_xmi_to_mt32_smf(data, seq.index) for seq in sequences]


def find_entry_offsets(data: bytes) -> List[int]:
    """Locate every embedded XMIDI stream by its FORM ... XDIR signature."""
    offsets = []
    for m in re.finditer(b"FORM", data):
        pos = m.start()
        if data[pos + 8:pos + 12] == b"XDIR":
            offsets.append(pos)
    return offsets


def split_entries(data: bytes) -> List[bytes]:
    """Split concatenated XMIDI streams from raw container data."""
    offsets = find_entry_offsets(data)
    if not offsets:
        raise ValueError("No XMIDI ('FORM ... XDIR') entries found.")

    entries = []
    for i, start in enumerate(offsets):
        end = offsets[i + 1] if i + 1 < len(offsets) else len(data)
        entries.append(data[start:end])
    return entries


# ===========================================================================
# Extraction and Lookup Table Handling
# ===========================================================================

def extract_songs(input_dir: Path, output_dir: Path):
    """Extract songs from FDMUS.DAT and save them using LOOKUP_TABLE filenames."""
    if input_dir.is_file():
        dat_path = input_dir
    else:
        dat_path = input_dir / "FDMUS.DAT"
        if not dat_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "FDMUS.DAT":
                    dat_path = child
                    break

    if not dat_path.exists():
        raise FileNotFoundError(f"FDMUS.DAT not found in {input_dir}")

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    dat_data = dat_path.read_bytes()
    entries = split_entries(dat_data)

    used_filenames = set()
    track_number = 0

    for entry_idx, xmi_payload in enumerate(entries):
        try:
            midis = convert_all(xmi_payload)
        except XmiError as e:
            print(f"Entry #{entry_idx:02d}: failed to convert - {e}")
            continue

        for seq_idx, midi_bytes in enumerate(midis):
            if track_number in LOOKUP_TABLE:
                base_name = LOOKUP_TABLE[track_number]
            elif str(track_number) in LOOKUP_TABLE:
                base_name = LOOKUP_TABLE[str(track_number)]
            else:
                if len(midis) > 1:
                    base_name = f"FDMUS_{entry_idx:02d}_{seq_idx:02d}"
                else:
                    base_name = f"FDMUS_{entry_idx:02d}"

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
            print(f"{filename}: {len(midi_bytes)} MIDI bytes (entry #{entry_idx:02d})")
            track_number += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input path to FDMUS.DAT or directory containing it (default: current directory)",
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