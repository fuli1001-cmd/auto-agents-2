"""Bound both wire bytes and decoder output before allocating document bodies."""
import zlib

from .errors import AA2Error

MAX_BODY = 4 * 1024 * 1024
ACQUISITION_TIMEOUT = 30


async def read_body(response, limit=MAX_BODY):
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise AA2Error("Unsupported document content encoding")
    data = bytearray()
    wire_size = 0
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    prefix = b""

    def decode(chunk):
        nonlocal decoder, prefix
        if encoding == "identity":
            output = chunk
        else:
            if decoder is None:
                prefix += chunk
                if len(prefix) < 2:
                    return
                wrapped = prefix[0] & 15 == 8 and int.from_bytes(prefix[:2], "big") % 31 == 0
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                chunk, prefix = prefix, b""
            try:
                output = decoder.decompress(chunk, limit - len(data) + 1)
            except zlib.error as exc:
                raise AA2Error("Invalid compressed document") from exc
            if decoder.unconsumed_tail or decoder.unused_data:
                raise AA2Error("Compressed document exceeds size limit or contains trailing data")
        if len(output) > limit - len(data):
            raise AA2Error("Document exceeds 4 MiB acquisition limit")
        data.extend(output)

    # A preloaded identity response is useful for in-process transports. Encoded
    # responses must retain their raw stream, otherwise the allocation already happened.
    if response.is_stream_consumed:
        if encoding != "identity":
            raise AA2Error("Encoded document requires a raw streaming response")
        decode(response.content)
    else:
        async for chunk in response.aiter_raw(chunk_size=65536):
            wire_size += len(chunk)
            if wire_size > limit:
                raise AA2Error("Encoded document exceeds 4 MiB acquisition limit")
            decode(chunk)
    if encoding != "identity" and (decoder is None or not decoder.eof):
        raise AA2Error("Truncated compressed document")
    return bytes(data)
