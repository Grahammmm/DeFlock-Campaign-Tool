"""Native MSG storage views, never invented standalone received message bytes."""
from dataclasses import dataclass
import hashlib
import io
import json
import re

SCHEMA = "native-msg-stream-manifest-v1"
MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


class NativeMSGError(ValueError):
    """Safe literal code."""


def require(condition, code="invalid_msg"):
    if not condition:
        raise NativeMSGError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n").encode()


@dataclass(frozen=True)
class NativeStream:
    locator: tuple[str, ...]
    payload: bytes

    @property
    def metadata(self):
        return {"ole_stream_locator": list(self.locator), "bytes": len(self.payload), "sha256": sha(self.payload)}


@dataclass(frozen=True)
class NativeItem:
    part: str
    parent_part: str
    locator: tuple[str, ...]
    manifest: bytes
    streams: tuple[NativeStream, ...]
    depth: int


@dataclass(frozen=True)
class NativeAttachment:
    part: str
    parent_part: str
    locator: tuple[str, ...]
    payload: bytes
    name: str | None
    depth: int


@dataclass(frozen=True)
class NativeInventory:
    items: tuple[NativeItem, ...]
    attachments: tuple[NativeAttachment, ...]
    units: tuple[dict, ...]
    directory_entries: int
    stream_bytes: int
    messages: int
    max_depth: int


def inventory(raw, *, max_parts=1000, max_captures=100, max_depth=32,
              max_stream_bytes=512*1024**2):
    require(type(raw) is bytes and raw.startswith(MAGIC))
    try:
        import extract_msg
        import olefile
    except ImportError:
        raise NativeMSGError("msg_decoder_unavailable") from None
    items, attachments, units = [], [], []
    stream_bytes, deepest, messages = 0, 1, 0
    try:
        with olefile.OleFileIO(io.BytesIO(raw), raise_defects=olefile.DEFECT_INCORRECT) as ole:
            stream_paths = [tuple(path) for path in ole.listdir(streams=True, storages=False)]
            storage_paths = [tuple(path) for path in ole.listdir(streams=False, storages=True)]
            paths = stream_paths + storage_paths
            require(len(paths) <= max_parts, "mail_wire_part_limit")
            require(len(set(tuple(p.casefold() for p in path) for path in paths)) == len(paths))
            require(all(path and all(type(p) is str and p and len(p) <= 31 and "/" not in p and "\\" not in p for p in path) for path in paths))
            streams, storages = set(stream_paths), set(storage_paths)
            visited = set()

            def read(path):
                nonlocal stream_bytes
                require(path in streams)
                size = ole.get_size(list(path))
                require(type(size) is int and 0 <= size <= 128*1024**2, "mail_wire_total_byte_limit")
                require(stream_bytes + size <= max_stream_bytes, "mail_wire_total_byte_limit")
                with ole.openstream(list(path)) as stream:
                    data = stream.read(size + 1)
                require(len(data) == size)
                stream_bytes += size
                return data

            def visit(message, prefix, context, depth):
                nonlocal deepest, messages
                require(depth <= max_depth, "mail_wire_depth_limit")
                require(prefix not in visited)
                visited.add(prefix)
                messages += 1
                deepest = max(deepest, depth)
                require(tuple(str(message.prefix).rstrip("/").split("/")) == prefix if prefix else not message.prefix)
                require(hasattr(message, "body"), "unsupported_msg_type")
                body = message.body or ""
                require(type(body) is str and len(body.encode("utf-8")) <= 64*1024**2, "msg_body_limit")
                headers = {key: str(getattr(message, key, None) or "") for key in ("subject", "sender", "to", "cc", "date")}
                require(sum(len(value) for value in headers.values()) <= 64*1024, "msg_body_limit")
                units.extend((
                    {"kind": "msg_headers", "locator": {"source_part": context, "ole_storage_locator": list(prefix)}, "text": "",
                     "data": {"headers": headers, "representation": "derived_msg_properties", "source_original_sha256": sha(raw)}},
                    {"kind": "msg_body", "locator": {"source_part": context, "ole_storage_locator": list(prefix)}, "text": body,
                     "data": {"representation": "derived_decoded_body", "source_original_sha256": sha(raw)}},
                ))
                expected = {path[-1] for path in storages if len(path) == len(prefix)+1 and path[:len(prefix)] == prefix and path[-1].startswith("__attach_version1.0_#")}
                seen = set()
                for attachment in message.attachments:
                    require(len(items) + len(attachments) < max_captures, "mail_wire_capture_limit")
                    directory = attachment.dir
                    require(type(directory) is str and re.fullmatch(r"__attach_version1\.0_#[0-9A-Fa-f]{8}", directory) is not None)
                    require(directory in expected and directory not in seen)
                    seen.add(directory)
                    path = prefix + (directory,)
                    data = attachment.data
                    name = attachment.longFilename or attachment.shortFilename or None
                    require(name is None or (type(name) is str and len(name) <= 4096 and "\x00" not in name))
                    if type(data) is bytes:
                        locator = path + ("__substg1.0_37010102",)
                        require(read(locator) == data)
                        part = "msg:" + "/".join(path)
                        attachments.append(NativeAttachment(part, context, locator, data, name, depth+1))
                        units.append({"kind": "msg_attachment", "locator": {"ole_stream_locator": list(locator), "source_part": context}, "text": "",
                                      "data": {"filename": name, "bytes_preserved": True, "needs_attachment_decoder": False, "sha256": sha(data)}})
                    else:
                        locator = path + ("__substg1.0_3701000D",)
                        require(locator in storages and hasattr(data, "prefix"), "unsupported_msg_attachment")
                        part = "native-msg:" + "/".join(locator)
                        native_streams = tuple(NativeStream(p, read(p)) for p in sorted(streams) if p[:len(locator)] == locator)
                        require(bool(native_streams))
                        manifest = canonical({"schema": SCHEMA, "source_original_sha256": sha(raw),
                                              "ole_storage_locator": list(locator), "streams": [s.metadata for s in native_streams]})
                        require(len(manifest) <= 1024*1024, "mail_wire_part_limit")
                        items.append(NativeItem(part, context, locator, manifest, native_streams, depth+1))
                        units.append({"kind": "msg_embedded_item", "locator": {"ole_storage_locator": list(locator), "source_part": context}, "text": "",
                                      "data": {"representation": "derived_embedded_msg_item", "source_original_sha256": sha(raw),
                                               "stream_manifest_sha256": sha(manifest), "bytes_preserved": True,
                                               "received_standalone_original": False}})
                        visit(data, locator, part, depth+1)
                require(seen == expected)

            with extract_msg.openMsg(io.BytesIO(raw)) as message:
                visit(message, (), "0", 1)
            return NativeInventory(tuple(items), tuple(attachments), tuple(units), len(paths),
                                   stream_bytes, messages, deepest)
    except NativeMSGError:
        raise
    except Exception:
        raise NativeMSGError("invalid_msg") from None
