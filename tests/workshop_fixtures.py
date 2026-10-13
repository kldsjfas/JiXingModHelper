"""Small, structurally valid UnityFS fixtures; their payloads are never executed."""
import lzma
import struct

import lz4.block


def text_asset(payload=b"resource-data", name="texture"):
    encoded = name.encode("utf-8")
    value = struct.pack("<i", len(encoded)) + encoded
    value += b"\0" * (-len(value) % 4)
    return value + struct.pack("<i", len(payload)) + payload


def serialized_file(objects=None, *, version=22):
    objects = [(49, text_asset())] if objects is None else objects
    header_size = 48 if version >= 22 else 20
    classes = list(dict.fromkeys(class_id for class_id, _ in objects))
    metadata = bytearray(b"2021.3.0f1\0" + struct.pack("<iBi", 19, 0, len(classes)))
    for class_id in classes:
        metadata.extend(struct.pack("<iBh", class_id, 0, -1))
        if class_id == 114:
            metadata.extend(b"\0" * 16)
        metadata.extend(b"\0" * 16)
    metadata.extend(struct.pack("<i", len(objects)))
    body = bytearray()
    for path_id, (class_id, data) in enumerate(objects, 1):
        metadata.extend(b"\0" * (-(header_size + len(metadata)) % 4))
        body.extend(b"\0" * (-len(body) % 8))
        metadata.extend(struct.pack("<qqIi" if version >= 22 else "<qIIi",
                                    path_id, len(body), len(data), classes.index(class_id)))
        body.extend(data)
    metadata.extend(struct.pack("<ii", 0, 0))  # script references and external files
    if version >= 20:
        metadata.extend(struct.pack("<i", 0))
    metadata.extend(b"\0")  # user information
    data_offset = header_size + len(metadata)
    data_offset += -data_offset % 16
    size = data_offset + len(body)
    if version >= 22:
        header = struct.pack(">4IB3sIqq8s", 0, 0, version, 0, 0, b"\0" * 3,
                             len(metadata), size, data_offset, b"\0" * 8)
    else:
        header = struct.pack(">4IB3s", len(metadata), size, version, data_offset, 0, b"\0" * 3)
    return header + metadata + b"\0" * (data_offset - header_size - len(metadata)) + body


def _compress(data, compression):
    if compression == 0:
        return data
    if compression in (2, 3):
        return lz4.block.compress(data, store_size=False)
    if compression == 1:
        dictionary = 1024 * 1024
        compressed = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[
            {"id": lzma.FILTER_LZMA1, "dict_size": dictionary, "lc": 3, "lp": 0, "pb": 2}])
        return struct.pack("<BI", 93, dictionary) + compressed
    raise ValueError("fixture compression is not supported")


def unityfs(nodes=None, *, compression=0, metadata_compression=0, version=7, at_end=False):
    nodes = [("CAB-test", serialized_file(), 4)] if nodes is None else nodes
    body = b"".join(data for _, data, _ in nodes)
    compressed = _compress(body, compression)
    directory = bytearray(b"\0" * 16 + struct.pack(">iIIHi", 1, len(body), len(compressed), compression, len(nodes)))
    offset = 0
    for name, data, flags in nodes:
        directory.extend(struct.pack(">qqI", offset, len(data), flags) + name.encode("utf-8") + b"\0")
        offset += len(data)
    packed_directory = _compress(bytes(directory), metadata_compression)
    prefix = b"UnityFS\0" + struct.pack(">I", version) + b"2021.3.0f1\0" * 2
    header_size = len(prefix) + 20
    padding = b"\0" * (-header_size % 16) if version >= 7 else b""
    size = header_size + len(padding) + len(packed_directory) + len(compressed)
    header = prefix + struct.pack(">qIII", size, len(packed_directory), len(directory),
                                  0x40 | metadata_compression | (0x80 if at_end else 0))
    content = compressed + packed_directory if at_end else packed_directory + compressed
    return header + padding + content


def resource_bundle(payload=b"resource-data", *, name="texture", compression=0):
    return unityfs([("CAB-test", serialized_file([(49, text_asset(payload, name))]), 4)],
                   compression=compression)


def pe_payload():
    data = bytearray(b"\0" * 160)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    return bytes(data)


def assembly_catalog(bundle_name="runtime.bundle", address="AstralParty.Runtime.dll"):
    names = [address, "runtime-dependency"]
    keys, offsets = bytearray(), []
    for name in names:
        offsets.append(len(keys))
        encoded = name.encode("ascii")
        keys.extend(b"\0" + struct.pack("<i", len(encoded)) + encoded)
    buckets = struct.pack("<i", 2)
    buckets += struct.pack("<iii", offsets[0], 1, 0) + struct.pack("<iii", offsets[1], 1, 1)
    entries = struct.pack("<i", 2)
    entries += struct.pack("<7i", 0, 0, 1, 0, 0, 0, 0)
    entries += struct.pack("<7i", 1, 0, -1, 0, 0, 0, 0)
    import base64
    return {"m_InternalIds": [address, "https://assets.invalid/" + bundle_name],
            "m_KeyDataString": base64.b64encode(keys).decode("ascii"),
            "m_BucketDataString": base64.b64encode(buckets).decode("ascii"),
            "m_EntryDataString": base64.b64encode(entries).decode("ascii")}
