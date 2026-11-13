#!/usr/bin/env python3
"""Convert OGG/Vorbis (and OGG/Opus) files to MP3 while preserving metadata.

Cover art is extracted byte-for-byte (from an attached picture stream or from
the Vorbis comment METADATA_BLOCK_PICTURE / COVERART fields) and embedded as an
ID3v2.3 APIC frame, so it is never re-encoded.

Only ffmpeg/ffprobe are required. No third-party Python packages.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import ctypes.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus"}

# Technical tags written by demuxers that are meaningless (or wrong) in the
# converted file.
IGNORED_TAGS = {
    "END",
    "ENDSERIAL",
    "ENDGRAN",
    "ENCODER",
    "ENCODED_BY",
    "DURATION",
    "BITRATE",
    "SIZE",
}

# Vorbis comment key -> ID3v2.3 text frame.
TEXT_FRAME_MAP = {
    "TITLE": "TIT2",
    "ARTIST": "TPE1",
    "ALBUM": "TALB",
    "ALBUMARTIST": "TPE2",
    "ALBUM_ARTIST": "TPE2",
    "COMPOSER": "TCOM",
    "GENRE": "TCON",
    "TRACKNUMBER": "TRCK",
    "TRACK": "TRCK",
    "DISCNUMBER": "TPOS",
    "DISC": "TPOS",
    "BPM": "TBPM",
    "COPYRIGHT": "TCOP",
    "LYRICIST": "TEXT",
    "CONDUCTOR": "TPE3",
    "REMIXER": "TPE4",
    "SUBTITLE": "TIT3",
    "ORIGINALARTIST": "TOPE",
    "ORIGINALALBUM": "TOAL",
    "ORIGINALDATE": "TORY",
    "ISRC": "TSRC",
    "GROUPING": "TIT1",
    "PUBLISHER": "TPUB",
    "COMPILATION": "TCMP",
}

PICTURE_KEYS = {"METADATA_BLOCK_PICTURE", "COVERART"}
COMMENT_KEYS = {"COMMENT", "DESCRIPTION"}
LYRICS_KEYS = {"LYRICS", "UNSYNCEDLYRICS"}

MIME_BY_CODEC = {
    "mjpeg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "bmp": "image/bmp",
    "gif": "image/gif",
    "webp": "image/webp",
    "tiff": "image/tiff",
}


class ConversionError(RuntimeError):
    pass


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path: Path) -> dict:
    proc = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if proc.returncode != 0:
        raise ConversionError(f"ffprobe failed for {path}:\n{proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ConversionError(f"Could not parse ffprobe output for {path}: {exc}") from exc


def _normalize_value(value) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value if v is not None)
    return str(value)


def collect_tags(info: dict) -> dict[str, str]:
    """Merge format and audio-stream tags, dropping technical junk."""
    tags: dict[str, str] = {}
    for source in (info.get("format", {}),):
        for key, value in (source.get("tags") or {}).items():
            tags[key.upper()] = _normalize_value(value)
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "audio":
            continue
        for key, value in (stream.get("tags") or {}).items():
            tags[key.upper()] = _normalize_value(value)
    return {
        key: value
        for key, value in tags.items()
        if key not in IGNORED_TAGS and key not in PICTURE_KEYS and value != ""
    }


# --------------------------------------------------------------------------- #
# Cover art extraction
# --------------------------------------------------------------------------- #


def iter_ogg_packets(data: bytes):
    """Yield raw packets from an Ogg bitstream."""
    offset = 0
    packet = bytearray()
    size = len(data)
    while True:
        start = data.find(b"OggS", offset)
        if start < 0 or start + 27 > size:
            return
        segments = data[start + 26]
        header_end = start + 27 + segments
        if header_end > size:
            return
        lacing = data[start + 27 : header_end]
        body_len = sum(lacing)
        body_end = header_end + body_len
        pos = header_end
        for segment in lacing:
            packet += data[pos : pos + segment]
            pos += segment
            if segment < 255:
                yield bytes(packet)
                packet.clear()
        offset = body_end if body_len else header_end


def parse_flac_picture(blob: bytes) -> dict | None:
    """Parse a FLAC PICTURE block (METADATA_BLOCK_PICTURE payload)."""
    try:
        off = 0
        pic_type = struct.unpack(">I", blob[off : off + 4])[0]
        off += 4
        mime_len = struct.unpack(">I", blob[off : off + 4])[0]
        off += 4
        mime = blob[off : off + mime_len].decode("ascii", "replace")
        off += mime_len
        desc_len = struct.unpack(">I", blob[off : off + 4])[0]
        off += 4
        description = blob[off : off + desc_len].decode("utf-8", "replace")
        off += desc_len
        width, height = struct.unpack(">II", blob[off : off + 8])
        off += 16  # width, height, colour depth, colour count
        data_len = struct.unpack(">I", blob[off : off + 4])[0]
        off += 4
        image = blob[off : off + data_len]
    except (struct.error, IndexError):
        return None
    if not image:
        return None
    return {
        "type": pic_type,
        "mime": mime,
        "description": description,
        "width": width,
        "height": height,
        "data": image,
    }


def parse_comment_packet(packet: bytes) -> dict[str, list[bytes]]:
    """Extract key -> raw values from a Vorbis/Opus comment packet."""
    result: dict[str, list[bytes]] = {}
    try:
        if packet.startswith(b"\x03vorbis"):
            off = 7
        elif packet.startswith(b"OpusTags"):
            off = 8
        else:
            return result
        vendor_len = struct.unpack("<I", packet[off : off + 4])[0]
        off += 4 + vendor_len
        count = struct.unpack("<I", packet[off : off + 4])[0]
        off += 4
        for _ in range(count):
            length = struct.unpack("<I", packet[off : off + 4])[0]
            off += 4
            comment = packet[off : off + length]
            off += length
            if b"=" not in comment:
                continue
            key, _, value = comment.partition(b"=")
            result.setdefault(key.decode("ascii", "replace").upper(), []).append(value)
    except (struct.error, IndexError):
        return result
    return result


def pictures_from_comments(comments: dict[str, list[bytes]]) -> list[dict]:
    pictures: list[dict] = []
    for raw in comments.get("METADATA_BLOCK_PICTURE", []):
        try:
            blob = base64.b64decode(raw, validate=False)
        except binascii.Error:
            continue
        picture = parse_flac_picture(blob)
        if picture:
            pictures.append(picture)

    cover_mime = ""
    if comments.get("COVERARTMIME"):
        cover_mime = comments["COVERARTMIME"][0].decode("ascii", "replace")
    for raw in comments.get("COVERART", []):
        try:
            image = base64.b64decode(raw, validate=False)
        except binascii.Error:
            continue
        if image:
            pictures.append(
                {
                    "type": 3,
                    "mime": cover_mime or guess_mime(image),
                    "description": "",
                    "data": image,
                }
            )
    return pictures


def guess_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def extract_attached_picture(path: Path, stream_index: int, codec: str) -> dict | None:
    """Pull an attached picture stream out of the container byte-for-byte."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-y",
            "-i",
            str(path),
            "-map",
            f"0:{stream_index}",
            "-c",
            "copy",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-",
        ],
        capture_output=True,
    )
    data = proc.stdout or b""
    if not data:
        return None
    return {
        "type": 3,
        "mime": MIME_BY_CODEC.get(codec.lower(), guess_mime(data)),
        "description": "",
        "data": data,
    }


def extract_cover(path: Path, info: dict) -> dict | None:
    pictures: list[dict] = []

    for stream in info.get("streams", []):
        if stream.get("codec_type") == "video" and (
            stream.get("disposition", {}).get("attached_pic")
        ):
            picture = extract_attached_picture(
                path, int(stream["index"]), stream.get("codec_name", "")
            )
            if picture:
                picture["source"] = "embedded"
                if stream.get("width"):
                    picture["width"] = stream["width"]
                if stream.get("height"):
                    picture["height"] = stream["height"]
                pictures.append(picture)

    if not pictures and path.suffix.lower() in AUDIO_EXTENSIONS:
        try:
            data = path.read_bytes()
        except OSError:
            data = b""
        if data:
            for packet in iter_ogg_packets(data):
                comments = parse_comment_packet(packet)
                if comments:
                    for picture in pictures_from_comments(comments):
                        picture["source"] = "embedded"
                        pictures.append(picture)

    if not pictures:
        # QQ Music / NetEase store the artwork as a custom Finder icon on macOS.
        return macos_icon_picture(path)

    # Prefer the front cover (type 3), then the first available picture.
    pictures.sort(key=lambda pic: 0 if pic.get("type") == 3 else 1)
    return pictures[0]


# --------------------------------------------------------------------------- #
# macOS custom file icon (QQ Music, NetEase Cloud Music, ...)
# --------------------------------------------------------------------------- #


def _load_libc():
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.getxattr.restype = ctypes.c_ssize_t
    libc.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    libc.setxattr.restype = ctypes.c_int
    return libc


def macos_getxattr(path: Path, name: str) -> bytes | None:
    if sys.platform != "darwin":
        return None
    libc = _load_libc()
    encoded_path = os.fsencode(path)
    encoded_name = name.encode("utf-8")
    size = libc.getxattr(encoded_path, encoded_name, None, 0, 0, 0)
    if size < 0:
        return None
    buffer = ctypes.create_string_buffer(size)
    read = libc.getxattr(encoded_path, encoded_name, buffer, size, 0, 0)
    if read < 0:
        return None
    return buffer.raw[:read]


def macos_setxattr(path: Path, name: str, value: bytes) -> bool:
    if sys.platform != "darwin":
        return False
    libc = _load_libc()
    buffer = ctypes.create_string_buffer(value, len(value))
    return libc.setxattr(os.fsencode(path), name.encode("utf-8"), buffer, len(value), 0, 0) == 0


def parse_resource_fork(data: bytes) -> dict[bytes, list[bytes]]:
    """Return resource type -> list of resource payloads."""
    resources: dict[bytes, list[bytes]] = {}
    try:
        data_off, map_off = struct.unpack(">II", data[:8])
        type_list_off = struct.unpack(">H", data[map_off + 24 : map_off + 26])[0]
        type_list = map_off + type_list_off
        num_types = struct.unpack(">H", data[type_list : type_list + 2])[0] + 1
        pos = type_list + 2
        for _ in range(num_types):
            res_type = data[pos : pos + 4]
            num_resources = struct.unpack(">H", data[pos + 4 : pos + 6])[0] + 1
            ref_list = type_list + struct.unpack(">H", data[pos + 6 : pos + 8])[0]
            pos += 8
            for _ in range(num_resources):
                res_offset = struct.unpack(">I", data[ref_list + 4 : ref_list + 8])[0] & 0xFFFFFF
                start = data_off + res_offset
                length = struct.unpack(">I", data[start : start + 4])[0]
                resources.setdefault(res_type, []).append(
                    data[start + 4 : start + 4 + length]
                )
                ref_list += 12
    except (struct.error, IndexError):
        return {}
    return resources


def largest_icns_png(icns: bytes) -> dict | None:
    """Pick the highest-resolution PNG representation inside an icns file."""
    if len(icns) < 8 or icns[:4] != b"icns":
        return None
    total = min(struct.unpack(">I", icns[4:8])[0], len(icns))
    best: dict | None = None
    offset = 8
    while offset + 8 <= total:
        element_type, element_len = struct.unpack(">4sI", icns[offset : offset + 8])
        if element_len < 8 or offset + element_len > total:
            break
        payload = icns[offset + 8 : offset + element_len]
        if payload.startswith(b"\x89PNG\r\n\x1a\n") and len(payload) >= 24:
            width, height = struct.unpack(">II", payload[16:24])
            if best is None or width * height > best["width"] * best["height"]:
                best = {
                    "type": 3,
                    "mime": "image/png",
                    "description": "Cover (front)",
                    "data": payload,
                    "width": width,
                    "height": height,
                    "source": "macOS icon",
                }
        offset += element_len
    return best


def macos_icon_picture(path: Path) -> dict | None:
    """Extract the custom Finder icon image stored in the resource fork."""
    fork = macos_getxattr(path, "com.apple.ResourceFork")
    if not fork:
        return None
    for icns in parse_resource_fork(fork).get(b"icns", []):
        picture = largest_icns_png(icns)
        if picture:
            return picture
    return None


def copy_macos_icon_attrs(source: Path, destination: Path) -> None:
    """Keep the Finder custom icon on the converted file as well."""
    if sys.platform != "darwin":
        return
    for name in ("com.apple.FinderInfo", "com.apple.ResourceFork"):
        value = macos_getxattr(source, name)
        if value:
            macos_setxattr(destination, name, value)


# --------------------------------------------------------------------------- #
# ID3v2.3 writer
# --------------------------------------------------------------------------- #


def synchsafe(value: int) -> bytes:
    return bytes(
        [
            (value >> 21) & 0x7F,
            (value >> 14) & 0x7F,
            (value >> 7) & 0x7F,
            value & 0x7F,
        ]
    )


def utf16(text: str) -> bytes:
    # ID3v2.3 encoding 0x01: UTF-16 with BOM, null-terminated.
    return b"\xff\xfe" + text.encode("utf-16-le") + b"\x00\x00"


def text_frame(frame_id: str, text: str) -> bytes:
    payload = b"\x01" + utf16(text)
    return frame_id.encode("ascii") + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def txxx_frame(description: str, value: str) -> bytes:
    payload = b"\x01" + utf16(description) + utf16(value)
    return b"TXXX" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def comm_frame(text: str, language: str = "eng") -> bytes:
    payload = (
        b"\x01"
        + language.encode("ascii", "replace")[:3].ljust(3, b"\x00")
        + utf16("")
        + utf16(text)
    )
    return b"COMM" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def uslt_frame(text: str, language: str = "eng") -> bytes:
    payload = (
        b"\x01"
        + language.encode("ascii", "replace")[:3].ljust(3, b"\x00")
        + utf16("")
        + utf16(text)
    )
    return b"USLT" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def apic_frame(picture: dict) -> bytes:
    mime = (picture.get("mime") or "image/jpeg").encode("latin-1", "replace")
    description = picture.get("description") or ""
    payload = (
        b"\x01"  # UTF-16 description; MIME stays ISO-8859-1
        + mime
        + b"\x00"
        + bytes([int(picture.get("type") or 3) & 0xFF])
        + utf16(description)
        + picture["data"]
    )
    return b"APIC" + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def build_id3(tags: dict[str, str], picture: dict | None) -> bytes:
    frames: list[bytes] = []

    title = tags.pop("TITLE", None)
    if title:
        frames.append(text_frame("TIT2", title))

    # Year: ID3v2.3 uses TYER with a four-digit value.
    for key in ("DATE", "YEAR"):
        value = tags.pop(key, None)
        if value:
            year = value[:4] if value[:4].isdigit() else value
            frames.append(text_frame("TYER", year))
            break

    used_frames: set[str] = set()
    for key, frame_id in TEXT_FRAME_MAP.items():
        value = tags.pop(key, None)
        if value and frame_id not in used_frames:
            frames.append(text_frame(frame_id, value))
            used_frames.add(frame_id)

    for key in COMMENT_KEYS:
        for value in _pop_all(tags, key):
            frames.append(comm_frame(value))

    for key in LYRICS_KEYS:
        for value in _pop_all(tags, key):
            frames.append(uslt_frame(value))

    for key, value in tags.items():
        if value:
            frames.append(txxx_frame(key, value))

    if picture and picture.get("data"):
        frames.append(apic_frame(picture))

    body = b"".join(frames)
    # 1 KiB of padding leaves room for editors to rewrite tags in place.
    body += b"\x00" * 1024
    header = b"ID3\x03\x00\x00" + synchsafe(len(body))
    return header + body


def _pop_all(tags: dict[str, str], key: str) -> list[str]:
    value = tags.pop(key, None)
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #


def strip_leading_id3v2(data: bytes) -> bytes:
    """Remove ID3v2 tags at the start of MP3 data (size is synchsafe)."""
    while data.startswith(b"ID3") and len(data) >= 10:
        size = (
            (data[6] & 0x7F) << 21
            | (data[7] & 0x7F) << 14
            | (data[8] & 0x7F) << 7
            | (data[9] & 0x7F)
        )
        footer = 10 if data[5] & 0x10 else 0
        data = data[10 + size + footer :]
    return data


def describe_cover(picture: dict | None) -> str:
    if not picture:
        return "no cover"
    size = len(picture["data"])
    size_text = f"{size / 1024:.0f} KiB" if size >= 1024 else f"{size} bytes"
    dimensions = ""
    if picture.get("width") and picture.get("height"):
        dimensions = f" {picture['width']}x{picture['height']}"
    origin = picture.get("source")
    origin_text = f" from {origin}" if origin and origin != "embedded" else ""
    return f"cover {picture['mime']}{dimensions}, {size_text}{origin_text}"


def convert_file(
    source: Path,
    destination: Path,
    bitrate: str,
    overwrite: bool,
    dry_run: bool,
) -> tuple[bool, str]:
    if destination.exists() and not overwrite and not dry_run:
        return False, f"skipped (exists): {destination.name}"

    info = probe(source)
    tags = collect_tags(info)
    picture = extract_cover(source, info)

    if dry_run:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return (
            True,
            f"would convert: {source.name}\n"
            f"    tags: {summary or '(none)'}\n"
            f"    {describe_cover(picture)}",
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".ogg2mp3-", suffix=".mp3", dir=destination.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        proc = run(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-map_metadata",
                "-1",
                "-c:a",
                "libmp3lame",
                "-b:a",
                bitrate,
                "-id3v2_version",
                "0",
                "-write_id3v1",
                "0",
                str(tmp_path),
            ]
        )
        if proc.returncode != 0:
            raise ConversionError(
                f"ffmpeg failed for {source.name}:\n{proc.stderr.strip()}"
            )

        id3 = build_id3(dict(tags), picture)
        with open(tmp_path, "r+b") as handle:
            # Be defensive: drop any ID3v2 tag ffmpeg may have written.
            audio = strip_leading_id3v2(handle.read())
            handle.seek(0)
            handle.write(id3)
            handle.write(audio)
            handle.truncate()

        os.replace(tmp_path, destination)
        # mkstemp creates 0600 files; match the user's default file mode.
        current_umask = os.umask(0)
        os.umask(current_umask)
        os.chmod(destination, 0o666 & ~current_umask)
        copy_macos_icon_attrs(source, destination)
    finally:
        tmp_path.unlink(missing_ok=True)

    art = f", {describe_cover(picture)}" if picture else ""
    return True, f"converted: {source.name} -> {destination.name} ({len(tags)} tags{art})"


def collect_inputs(paths: list[str], recursive: bool) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            files.extend(
                item for item in iterator if item.is_file() and item.suffix.lower() in AUDIO_EXTENSIONS
            )
        elif path.is_file():
            files.append(path)
        else:
            print(f"warning: not found: {path}", file=sys.stderr)
    return sorted(dict.fromkeys(files))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Convert OGG/Vorbis/Opus files to MP3, preserving metadata and cover art."
    )
    parser.add_argument("paths", nargs="+", help="OGG files or directories")
    parser.add_argument(
        "-o", "--output-dir", help="write MP3 files here (default: next to each source)"
    )
    parser.add_argument(
        "-b", "--bitrate", default="320k", help="MP3 bitrate (default: 320k)"
    )
    parser.add_argument("-r", "--recursive", action="store_true", help="recurse into directories")
    parser.add_argument("--overwrite", action="store_true", help="replace existing MP3 files")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be converted, without writing"
    )
    args = parser.parse_args()

    files = collect_inputs(args.paths, args.recursive)
    if not files:
        print("No OGG files found.", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else None
    converted = skipped = failed = 0

    for source in files:
        destination = (
            output_dir / f"{source.stem}.mp3" if output_dir else source.with_suffix(".mp3")
        )
        try:
            ok, message = convert_file(
                source, destination, args.bitrate, args.overwrite, args.dry_run
            )
        except ConversionError as exc:
            failed += 1
            print(f"error: {exc}", file=sys.stderr)
            continue
        except OSError as exc:
            failed += 1
            print(f"error: {source}: {exc}", file=sys.stderr)
            continue

        if ok:
            converted += 1
        else:
            skipped += 1
        print(message)

    if not args.dry_run:
        print(
            f"\nDone: {converted} converted, {skipped} skipped, {failed} failed.",
            file=sys.stderr,
        )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
