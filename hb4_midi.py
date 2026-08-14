#!/usr/bin/env python3
'''
HardBall 4

Extracts General MIDI (GM) BGM tracks from HardBall 4 (1994).

Unpacking and File Conversion Structure:
Reads HARDBALL.BIN from the specified directory and carves embedded Human
Machine Interfaces MIDI (HMIMIDIP / HMI) music streams. Each HMI block is parsed,
decoding variable-length delta times and MIDI event streams across all tracks.
The tracks are converted into Standard MIDI File (SMF Type 1) format (.MID)
with timing calibrated to preserve original playback speed based on the
header tick rate.
'''

import argparse
from pathlib import Path
import struct

FOLDER_NAME = "HardBall 4 (1994) [GM]"

LOOKUP_TABLE = {}

HMIMIDIP_SIG = b"HMIMIDIP"
DEFAULT_PPQN = 192
DEFAULT_TICK_HZ = 120.0
LEGACY_TEMPO_US = 0x188000
LAST_ENTRY_FALLBACK_MAX = 200_000


class HmpParseError(Exception):
    pass


def clamp_tempo_us(tempo_us: int) -> int:
    return max(1, min(0xFFFFFF, int(tempo_us)))


def tempo_us_to_meta(tempo_us: int) -> bytes:
    return clamp_tempo_us(tempo_us).to_bytes(3, "big")


def choose_timing(buf: bytes, is_funky: bool):
    ppqn = DEFAULT_PPQN

    if is_funky:
        if len(buf) > 0x4D:
            dtx = (buf[0x4C] << 16) | buf[0x4D]
            if 1 <= dtx <= 0x7FFF:
                ppqn = dtx
        return ppqn, LEGACY_TEMPO_US, None

    tick_hz = DEFAULT_TICK_HZ
    if len(buf) >= 0x3C:
        header_tick = int.from_bytes(buf[0x38:0x3C], "little")
        if 10 <= header_tick <= 10000:
            tick_hz = float(header_tick)

    tempo_us = round(ppqn * 1_000_000 / tick_hz)
    tempo_us = clamp_tempo_us(tempo_us)
    return ppqn, tempo_us, tick_hz


def find_all(data: bytes, sig: bytes):
    idxs = []
    pos = 0
    while True:
        idx = data.find(sig, pos)
        if idx == -1:
            break
        idxs.append(idx)
        pos = idx + 1
    return idxs


def read_declared_len(data: bytes, idx: int):
    if idx + 0x24 > len(data):
        return None

    le = struct.unpack("<I", data[idx + 0x20:idx + 0x24])[0]
    if 500 < le < 200_000:
        return le

    be = struct.unpack(">I", data[idx + 0x20:idx + 0x24])[0]
    if 500 < be < 200_000:
        return be

    return None


def carve_hmimidip(data: bytes):
    idxs = find_all(data, HMIMIDIP_SIG)
    chunks = []

    for i, idx in enumerate(idxs):
        has_next = (i + 1) < len(idxs)

        if has_next:
            end = idxs[i + 1]
        else:
            declared = read_declared_len(data, idx)
            if declared is not None:
                end = min(len(data), idx + declared + 0x1000)
            else:
                end = min(len(data), idx + LAST_ENTRY_FALLBACK_MAX)

        if end <= idx:
            continue

        chunk = data[idx:end]
        default_stem = f"HARDBALL_{len(chunks):02d}"
        chunks.append((idx, chunk, default_stem))

    return chunks


def is_hmp(buf: bytes):
    if len(buf) < 8:
        return False
    if buf[0:7] != b"HMIMIDI":
        return False
    return buf[7] in (ord("P"), ord("R"))


def decode_hmp_delta(buf: bytes, pos: int, end: int):
    delta = 0
    shift = 0
    while True:
        if pos >= end:
            return 0, pos
        b = buf[pos]
        pos += 1
        delta += (b & 0x7F) << shift
        shift += 7
        if b & 0x80:
            break
    return delta, pos


def decode_delta_std(buf: bytes, pos: int, end: int):
    value = 0
    while True:
        if pos >= end:
            return -1, pos
        b = buf[pos]
        pos += 1
        value = (value << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return value, pos


def encode_vlq(value: int) -> bytes:
    if value == 0:
        return bytes([0])
    out = []
    out.append(value & 0x7F)
    value >>= 7
    while value:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    return bytes(reversed(out))


def parse_hmp(buf: bytes):
    if not is_hmp(buf):
        raise HmpParseError("Not a valid HMIMIDIP/HMIMIDIR signature")

    is_funky = (buf[7] == ord("R"))
    offset = 0x1A if is_funky else 0x30

    if offset >= len(buf):
        raise HmpParseError("Buffer too short for track count offset")

    track_count = buf[offset]
    if track_count < 1:
        raise HmpParseError(f"Invalid track count: {track_count}")

    ppqn, header_tempo_us, tick_hz = choose_timing(buf, is_funky)

    pos = offset
    end = len(buf)
    prev = buf[pos] if pos < end else None
    pos += 1
    found = False

    while pos < end:
        if prev != 0xFF:
            prev = buf[pos]
            pos += 1
            continue

        cur = buf[pos]
        pos += 1
        if cur != 0x2F:
            prev = cur
            continue

        found = True
        break

    if not found:
        raise HmpParseError("End marker (FF 2F) not found in master track")

    skip = 3 if is_funky else 5
    pos += skip

    if pos > end:
        raise HmpParseError("Offset exceeds buffer after master track")

    real_tracks = []
    has_tempo_at_tick_zero = False

    for _ in range(1, track_count):
        if is_funky:
            if end - pos < 4:
                break
            track_size = struct.unpack("<H", buf[pos:pos + 2])[0]
            pos += 2
            track_size -= 4
            if track_size < 0:
                break
            if end - pos < track_size + 2:
                break
            pos += 2
        else:
            if end - pos < 8:
                break
            track_size = struct.unpack("<I", buf[pos:pos + 4])[0]
            pos += 4
            track_size -= 12
            if track_size < 0:
                break
            if end - pos < track_size + 8:
                break
            pos += 4

        track_end = min(end, pos + track_size)
        events = []
        cur_tick = 0
        p = pos

        while p < track_end:
            delta, p = decode_hmp_delta(buf, p, track_end)
            cur_tick += delta

            if p >= track_end:
                break

            status = buf[p]
            p += 1

            if status == 0xFF:
                if p >= track_end:
                    break
                meta_type = buf[p]
                p += 1
                meta_len, p = decode_delta_std(buf, p, track_end)
                if meta_len < 0 or track_end - p < meta_len:
                    break

                meta_data = buf[p:p + meta_len]
                p += meta_len

                full_meta = (
                    b"\xFF"
                    + bytes([meta_type])
                    + encode_vlq(meta_len)
                    + meta_data
                )
                events.append((cur_tick, full_meta))

                if meta_type == 0x51 and meta_len == 3 and cur_tick == 0:
                    has_tempo_at_tick_zero = True

                if meta_type == 0x2F:
                    break

            elif 0x80 <= status <= 0xEF:
                nbytes = 1 if (status & 0xF0) in (0xC0, 0xD0) else 2
                if track_end - p < nbytes:
                    break

                data = buf[p:p + nbytes]
                p += nbytes

                if any(b > 0x7F for b in data):
                    data = bytes(b & 0x7F for b in data)

                events.append((cur_tick, bytes([status]) + data))
            else:
                break

        if not events or events[-1][1][0:2] != b"\xFF\x2F":
            events.append((cur_tick, b"\xFF\x2F\x00"))

        real_tracks.append(events)

        trailer = 0 if is_funky else 4
        pos = min(end, track_end + trailer)

    conductor_events = []
    if not has_tempo_at_tick_zero:
        conductor_events.append(
            (0, b"\xFF\x51\x03" + tempo_us_to_meta(header_tempo_us))
        )
    conductor_events.append((0, b"\xFF\x2F\x00"))
    real_tracks.insert(0, conductor_events)

    return ppqn, real_tracks, header_tempo_us, tick_hz


def make_smf1(ppqn: int, tracks):
    mtrk_chunks = []
    for events in tracks:
        body = bytearray()
        prev_tick = 0
        for tick, data in events:
            delta = tick - prev_tick
            if delta < 0:
                delta = 0
            body += encode_vlq(delta)
            body += data
            prev_tick = tick
        mtrk_chunks.append(b"MTrk" + struct.pack(">I", len(body)) + bytes(body))

    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(mtrk_chunks), ppqn)
    return header + b"".join(mtrk_chunks)


def extract_songs(input_dir: Path, output_dir: Path):
    """Extract songs from HARDBALL.BIN and save them using LOOKUP_TABLE filenames."""
    if input_dir.is_file():
        bin_path = input_dir
    else:
        bin_path = input_dir / "HARDBALL.BIN"
        if not bin_path.exists():
            for child in input_dir.iterdir():
                if child.name.upper() == "HARDBALL.BIN":
                    bin_path = child
                    break

    if not bin_path.exists():
        raise FileNotFoundError(f"HARDBALL.BIN not found in {input_dir}")

    target_dir = output_dir / FOLDER_NAME
    target_dir.mkdir(parents=True, exist_ok=True)

    data = bin_path.read_bytes()
    chunks = carve_hmimidip(data)

    if not chunks:
        raise ValueError(f"No HMI/HMP tracks found in {bin_path}")

    used_filenames = set()

    for number, (idx, chunk, default_stem) in enumerate(chunks):
        try:
            ppqn, tracks, tempo_us, tick_hz = parse_hmp(chunk)
            smf_data = make_smf1(ppqn, tracks)
        except HmpParseError as e:
            print(f"Skipping track at 0x{idx:X}: {e}")
            continue

        if number in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[number]
        elif default_stem in LOOKUP_TABLE:
            base_name = LOOKUP_TABLE[default_stem]
        else:
            base_name = default_stem

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
        out_path.write_bytes(smf_data)
        print(f"{filename}: {len(smf_data)} MIDI bytes (PPQN={ppqn}, tempo={tempo_us}us)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        type=Path,
        nargs="?",
        default=Path("."),
        help="Input directory containing HARDBALL.BIN (default: current directory)",
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