#!/usr/bin/env python3
'''
Lakers vs. Celtics and the NBA Playoffs

Extracts Roland MT-32 BGM tracks from Lakers vs. Celtics and the NBA Playoffs (1989).

Unpacking and File Conversion Structure:
Reads the sound driver overlay ('M') and song files ('S', 'O', 'R', 'V') from the
specified input directory. The MT-32 driver 'M' is decoded from its 3-byte XOR
obfuscation to dynamically extract Roland MT-32 System Exclusive setup data
(timbre memory parameters, reverb settings, patch-to-timbre mappings, and the
display message) as well as the 60-entry program change translation table.
Each MUS file begins with a header containing initial channel volumes and an offset
to the event stream. The event stream contains channel voice messages with trailing
1-byte delta times based on the PIT timer tick rate (~72.82 Hz). These events are
parsed, translated to proper MT-32 patch numbers, and converted into Standard MIDI
Format (SMF Type 0) files.
'''

import argparse
import struct
from fractions import Fraction
from pathlib import Path

FOLDER_NAME = "Lakers vs. Celtics and the NBA Playoffs (1989) [MT-32]"

LOOKUP_TABLE = {
    'S': 'START',
    'O': 'OPTIONS',
    'R': 'REPORT',
    'V': 'VICTORY',
}

# Header offsets in EA MUS files
HDR_ROLAND_ENABLE_OFF = 0x02
HDR_ROLAND_ENABLE_LEN = 16
HDR_ROLAND_VOLUME_OFF = 0x12
HDR_ROLAND_VOLUME_LEN = 16

# Driver timing constants (PIT timer channel 0 with divisor 0x4000)
PIT_BASE_HZ = Fraction(1193182)
PIT_DIVISOR = 16384
FRAME_HZ = PIT_BASE_HZ / PIT_DIVISOR  # ~72.821 Hz

MIDI_DIVISION = 1000
MIDI_TEMPO_USEC = round(MIDI_DIVISION * Fraction(1_000_000) / FRAME_HZ)

# Length of data bytes by status high nibble
DATA_LEN = {
    0x8: 2,  # Note Off
    0x9: 2,  # Note On
    0xA: 2,  # Polyphonic Aftertouch
    0xB: 2,  # Control Change
    0xC: 1,  # Program Change
    0xD: 1,  # Channel Aftertouch
    0xE: 2,  # Pitch Bend
}

# Bounds of the Program-Change translation table in the decoded driver
PROGRAM_XLAT_OFFSET = 0x14
PROGRAM_XLAT_LEN = 60


def write_vlq(value: int) -> bytes:
    """Encode an integer as a MIDI variable-length quantity."""
    if not 0 <= value <= 0x0FFFFFFF:
        raise ValueError(f"MIDI delta time out of range: {value}")
    encoded = [value & 0x7F]
    value >>= 7
    while value:
        encoded.append(0x80 | (value & 0x7F))
        value >>= 7
    return bytes(reversed(encoded))


def decode_mt32_driver(raw: bytes) -> bytes:
    """Decode the 3-byte-block XOR obfuscation used on sound driver overlay files."""
    n = len(raw)
    if n % 3 != 0:
        raise ValueError(f"Driver file size {n} is not a multiple of 3")
    buf = bytearray(raw)
    i = 0
    while i + 2 < n:
        a, b, c = buf[i], buf[i + 1], buf[i + 2]
        buf[i] = c ^ b
        buf[i + 2] = a ^ b
        i += 3
    return bytes(buf)


def roland_sysex(block: bytes) -> bytes:
    """Wrap a Roland address + payload block into a full DT1 SysEx message."""
    checksum = (-sum(block)) & 0x7F
    return bytes([0x41, 0x10, 0x16, 0x12]) + block + bytes([checksum, 0xF7])


def extract_mt32_sysex(driver_data: bytes) -> list:
    """Extract MT-32 startup SysEx blocks from decoded driver overlay data."""
    def extract_block(offset):
        end = driver_data.index(0xFF, offset)
        return driver_data[offset:end], end

    blocks = []

    # 1) Timbre Memory chain
    si = 0x63A
    while si < len(driver_data) and driver_data[si] != 0x80:
        blk, end = extract_block(si)
        blocks.append(blk)
        si = end + 1

    # 2) Reverb setup
    blocks.append(extract_block(0x85)[0])

    # 3) Patch memory -> timbre mappings
    for off in (0x8C, 0xB0, 0xC2, 0x10A, 0xD4, 0x11C):
        blocks.append(extract_block(off)[0])

    # 4) Display message
    blocks.append(extract_block(0x70)[0])

    return [bytes([0xF0]) + roland_sysex(b) for b in blocks]


def extract_program_xlat(driver_data: bytes) -> bytes:
    """Extract raw MUS program to MT-32 patch translation table from driver data."""
    table = driver_data[PROGRAM_XLAT_OFFSET: PROGRAM_XLAT_OFFSET + PROGRAM_XLAT_LEN]
    sentinel = driver_data[PROGRAM_XLAT_OFFSET + PROGRAM_XLAT_LEN]
    if sentinel != 0xFF:
        raise ValueError(
            f"Program translation table sentinel mismatch: expected 0xFF at offset "
            f"0x{PROGRAM_XLAT_OFFSET + PROGRAM_XLAT_LEN:X}, got 0x{sentinel:02X}"
        )
    return table


class MusEvent:
    __slots__ = ("status", "data", "delta")

    def __init__(self, status, data, delta):
        self.status = status
        self.data = data
        self.delta = delta


def parse_mus(data: bytes):
    """Parse EA MUS file into header settings, event list, and loop status."""
    if len(data) < 2:
        raise ValueError("File too short to contain a MUS header")

    song_start = struct.unpack_from("<H", data, 0)[0]
    if song_start >= len(data):
        raise ValueError(
            f"Header indicates song data starts at 0x{song_start:X}, "
            f"but file is only {len(data)} bytes long"
        )

    roland_enable = data[
        HDR_ROLAND_ENABLE_OFF: HDR_ROLAND_ENABLE_OFF + HDR_ROLAND_ENABLE_LEN
    ]
    roland_volume = data[
        HDR_ROLAND_VOLUME_OFF: HDR_ROLAND_VOLUME_OFF + HDR_ROLAND_VOLUME_LEN
    ]

    pos = song_start
    n = len(data)
    running_status = None
    events = []
    loop = False

    while pos < n:
        b = data[pos]
        pos += 1

        if b & 0x80:
            status = b
            if status == 0xFC:
                if pos >= n:
                    raise ValueError("Truncated file: missing loop/end byte after 0xFC")
                loop_byte = data[pos]
                pos += 1
                loop = (loop_byte == 0x80)
                break
            running_status = status
            hi = status >> 4
            dlen = DATA_LEN.get(hi, 2)
            if pos + dlen > n:
                raise ValueError(f"Truncated event at offset 0x{pos - 1:X}")
            event_data = data[pos: pos + dlen]
            pos += dlen
        else:
            if running_status is None:
                raise ValueError(
                    f"Running status used before status byte seen at offset 0x{pos - 1:X}"
                )
            status = running_status
            hi = status >> 4
            dlen = DATA_LEN.get(hi, 2)
            rest_len = dlen - 1
            if pos + rest_len > n:
                raise ValueError(f"Truncated event at offset 0x{pos - 1:X}")
            event_data = bytes([b]) + data[pos: pos + rest_len]
            pos += rest_len

        if pos >= n:
            raise ValueError(f"Truncated file: missing delta byte at offset 0x{pos:X}")
        delta = data[pos]
        pos += 1

        events.append(MusEvent(status, event_data, delta))
    else:
        raise ValueError("Reached end of file without finding 0xFC end marker")

    return song_start, roland_enable, roland_volume, events, loop


def build_track(events, roland_enable, roland_volume, loop, track_name,
                mt32_sysex, program_xlat) -> bytes:
    """Construct one MIDI track containing MT-32 setup, volume CCs, and note events."""
    out = bytearray()

    def write_event(delta, event_bytes):
        out.extend(write_vlq(delta))
        out.extend(event_bytes)

    def write_sysex(delta, msg):
        assert msg[0] == 0xF0 and msg[-1] == 0xF7
        rest = msg[1:]
        write_event(delta, bytes([0xF0]) + write_vlq(len(rest)) + rest)

    # Track Name
    name_bytes = track_name.encode("ascii", "replace")
    write_event(0, bytes([0xFF, 0x03, len(name_bytes)]) + name_bytes)

    # Set Tempo
    tempo = MIDI_TEMPO_USEC
    write_event(0, bytes([0xFF, 0x51, 0x03]) + tempo.to_bytes(3, "big"))

    # Loop start marker
    if loop:
        marker = b"loop_start"
        write_event(0, bytes([0xFF, 0x06, len(marker)]) + marker)

    # Roland MT-32 SysEx instrument definitions and config
    if mt32_sysex:
        for msg in mt32_sysex:
            write_sysex(0, msg)

    # Initial channel volumes
    for ch in range(16):
        if ch < len(roland_enable) and roland_enable[ch] and ch < len(roland_volume):
            vol = roland_volume[ch] & 0x7F
            write_event(0, bytes([0xB0 | ch, 0x07, vol]))

    # Event stream (shift trailing delta in MUS to leading delta in SMF)
    pending_delta = 0
    for ev in events:
        data = ev.data
        if (ev.status & 0xF0) == 0xC0 and program_xlat is not None:
            raw_prog = data[0]
            if raw_prog < len(program_xlat):
                data = bytes([program_xlat[raw_prog]])
        write_event(pending_delta, bytes([ev.status]) + data)
        pending_delta = ev.delta

    # End of Track
    write_event(pending_delta, bytes([0xFF, 0x2F, 0x00]))

    return bytes(out)


def make_smf(division: int, track: bytes) -> bytes:
    """Wrap a single MIDI track into Standard MIDI File (SMF Type 0) format."""
    return (
        b"MThd"
        + struct.pack(">IHHH", 6, 0, 1, division)
        + b"MTrk"
        + struct.pack(">I", len(track))
        + track
    )


def find_file_case_insensitive(directory: Path, target_name: str):
    """Find a file in the given directory matching target_name (ignoring case/extension)."""
    target_upper = target_name.upper()
    for child in directory.iterdir():
        if child.is_file() and (
            child.name.upper() == target_upper or child.stem.upper() == target_upper
        ):
            return child
    return None


def extract_songs(input_dir: Path, output_dir: Path):
    """Extract and convert songs from input_dir using LOOKUP_TABLE into output_dir."""
    if not input_dir.exists() or not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory not found: {input_dir}")

    # Find driver file 'M' (case-insensitive)
    driver_file = find_file_case_insensitive(input_dir, "M")
    if not driver_file:
        raise FileNotFoundError(
            f"MT-32 driver file 'M' not found in input directory: {input_dir}"
        )

    # Decode driver and extract SysEx & translation table
    driver_raw = driver_file.read_bytes()
    decoded_driver = decode_mt32_driver(driver_raw)
    mt32_sysex = extract_mt32_sysex(decoded_driver)
    program_xlat = extract_program_xlat(decoded_driver)

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    extracted_count = 0
    for key, song_name in LOOKUP_TABLE.items():
        song_file = find_file_case_insensitive(input_dir, key)
        if not song_file:
            print(f"Skipping {key}: file not found in {input_dir}")
            continue

        raw_song_data = song_file.read_bytes()
        song_start, roland_enable, roland_volume, events, loop = parse_mus(raw_song_data)

        filename = f"{song_name}.MID"
        track_bytes = build_track(
            events,
            roland_enable,
            roland_volume,
            loop,
            song_name,
            mt32_sysex,
            program_xlat,
        )

        smf_data = make_smf(MIDI_DIVISION, track_bytes)
        out_path = target_dir / filename
        out_path.write_bytes(smf_data)

        total_frames = sum(ev.delta for ev in events)
        duration_sec = float(total_frames / FRAME_HZ)
        print(f"{filename}: {len(track_bytes)} MIDI bytes, ~{duration_sec:.1f}s, loop={loop}")
        extracted_count += 1

    if extracted_count == 0:
        print("No matching MUS files were found to convert.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing MT-32 driver 'M' and song files (default: current directory)",
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