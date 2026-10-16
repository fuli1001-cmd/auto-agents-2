"""Bound wire input and decoded output, including ambiguous HTTP deflate streams."""
import zlib

from .errors import AA2Error

MAX_BODY = 4 * 1024 * 1024
ACQUISITION_TIMEOUT = 30


async def read_body(response, limit=MAX_BODY):
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise AA2Error("Unsupported document content encoding")
    wire = bytearray()
    if response.is_stream_consumed:
        if encoding != "identity":
            raise AA2Error("Encoded document requires a raw streaming response")
        if len(response.content) > limit:
            raise AA2Error("Document exceeds acquisition limit")
        wire.extend(response.content)
    else:
        async for chunk in response.aiter_raw(chunk_size=65536):
            if len(chunk) > limit - len(wire):
                raise AA2Error("Encoded document exceeds acquisition limit")
            wire.extend(chunk)
    if encoding == "identity":
        return bytes(wire)
    # Deflate has two deployed formats whose first bytes can coincide. Buffer only
    # bounded raw input; never publish any output from a failed decoding attempt.
    formats = (16 + zlib.MAX_WBITS,) if encoding == "gzip" else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    failures = set()
    for bits in formats:
        decoder = zlib.decompressobj(bits)
        try:
            output = decoder.decompress(wire, limit + 1)
        except zlib.error:
            continue
        if len(output) > limit or decoder.unconsumed_tail:
            failures.add("limit")
        elif decoder.unused_data:
            failures.add("trailing")
        elif not decoder.eof:
            failures.add("truncated")
        else:
            return output
    if "limit" in failures:
        raise AA2Error("Decoded document exceeds acquisition limit")
    if "truncated" in failures:
        raise AA2Error("Truncated compressed document")
    raise AA2Error("Invalid compressed document or trailing data")
