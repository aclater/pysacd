"""The SACD Ripper network protocol (PS3 server on TCP port 2002), client and simulator.

`sacd_extract -i HOST:2002` talks to the SACD Ripper homebrew running on a PS3. The PS3 reads
and decrypts the disc; on the wire there are only plain 2048-byte sectors. This module speaks
the same protocol, so pysacd can read from a PS3 (`RemoteImage`) and so the protocol can be
tested without one (`PS3Simulator`, which serves an already-decrypted ISO).

Wire format, from sacd-ripper's `libs/libsacd/sacd_ripper.proto`, `sacd_input.c` and the PS3
side in `src/server.c`:

- Each message is a protobuf (proto2) message with no length prefix, followed by one zero
  byte. nanopb treats a zero tag as end of message, so the zero byte delimits messages.
- ServerRequest:  1 type (varint), 2 sector_offset (varint), 3 sector_count (varint).
  Types: 1 DISC_OPEN, 2 DISC_CLOSE, 3 DISC_READ, 4 DISC_SIZE.
- ServerResponse: 1 type (varint), 2 result (int64 varint), 3 data (bytes, optional).
  Types: 1 DISC_OPENED, 2 DISC_CLOSED, 3 DISC_READ, 4 DISC_SIZE.
- DISC_OPEN: result 0 when the disc opened and authenticated.
- DISC_SIZE: result is the total number of 2048-byte sectors.
- DISC_READ: result is the number of sectors returned (`data` holds result * 2048 bytes).
  A read returns at most 512 sectors (MAX_PROCESSING_BLOCK_SIZE) and is cut short at the
  boundaries of the audio areas, because the PS3 decrypts area data separately. Callers must
  loop on short reads.
- DISC_CLOSE: result 0, then the server closes the connection.
"""

from __future__ import annotations

import io
import os
import socket
import socketserver
import threading
import time
from collections.abc import Sequence

from .toc import SECTOR_SIZE, SACDError, SACDImage

DEFAULT_PORT = 2002
MAX_BLOCK = 512                 # MAX_PROCESSING_BLOCK_SIZE in sacd-ripper's scarletbook.h

# Network data is untrusted: bound every length before allocating.
MAX_FIELD = MAX_BLOCK * SECTOR_SIZE + 1024      # largest legitimate field: one 512-sector read
MAX_FIELDS = 16                                 # the protocol's messages have 3 fields

REQ_OPEN, REQ_CLOSE, REQ_READ, REQ_SIZE = 1, 2, 3, 4
RESP_OPENED, RESP_CLOSED, RESP_READ, RESP_SIZE = 1, 2, 3, 4


class RemoteError(SACDError):
    pass


# --- protobuf (just the subset this protocol uses) -------------------------------------------

def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1          # int64 negatives are sent as 10-byte two's complement
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _signed64(n: int) -> int:
    return n - (1 << 64) if n >= 1 << 63 else n


def encode_message(fields: Sequence[tuple[int, int | bytes]]) -> bytes:
    """Encode (field number, value) pairs - ints as varints, bytes as length-delimited - plus the 0 terminator."""
    out = bytearray()
    for num, val in fields:
        if isinstance(val, (bytes, bytearray, memoryview)):
            out += _varint(num << 3 | 2) + _varint(len(val)) + bytes(val)
        else:
            out += _varint(num << 3 | 0) + _varint(val)
    out.append(0)
    return bytes(out)


class _Reader:
    """Buffered reads from a socket, for decoding unframed messages."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()

    def take(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise EOFError("connection closed")
            self.buf += chunk
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def varint(self) -> int:
        shift = val = 0
        while True:
            b = self.take(1)[0]
            val |= (b & 0x7F) << shift
            if not b & 0x80:
                return val
            shift += 7
            if shift > 63:
                raise RemoteError("malformed varint")


def decode_message(r: _Reader) -> dict[int, int | bytes]:
    fields: dict[int, int | bytes] = {}
    for _ in range(MAX_FIELDS + 1):
        key = r.varint()
        if key == 0:
            return fields
        num, wt = key >> 3, key & 7
        if wt == 0:
            fields[num] = r.varint()
        elif wt == 2:
            n = r.varint()
            if n > MAX_FIELD:
                raise RemoteError(f"field {num} is {n} bytes (limit {MAX_FIELD})")
            fields[num] = r.take(n)
        elif wt == 1:
            fields[num] = int.from_bytes(r.take(8), "little")
        elif wt == 5:
            fields[num] = int.from_bytes(r.take(4), "little")
        else:
            raise RemoteError(f"unsupported wire type {wt}")
    raise RemoteError(f"more than {MAX_FIELDS} fields in one message")


# --- client ------------------------------------------------------------------------------------

def parse_address(spec: str) -> tuple[str, int] | None:
    """'host:port' or 'host:' style address as accepted by sacd_extract -i; None if it looks like a path."""
    if os.path.exists(spec) or ":" not in spec or spec.startswith(("/", ".")):
        return None
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


class PS3Client:
    """Talks to a SACD Ripper server (a PS3, or PS3Simulator)."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 60.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            raise RemoteError(f"cannot connect to {host}:{port}: {e}") from e
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.reader = _Reader(self.sock)
        self.address = f"{host}:{port}"
        self._open = False

    def _call(self, req_type: int, offset: int = 0, count: int = 0, want: int = 0) -> dict:
        self.sock.sendall(encode_message([(1, req_type), (2, offset), (3, count)]))
        try:
            resp = decode_message(self.reader)
        except (EOFError, OSError) as e:
            raise RemoteError(f"{self.address}: {e}") from e
        if resp.get(1) != want:
            raise RemoteError(f"{self.address}: unexpected response type {resp.get(1)!r} (wanted {want})")
        result = resp.get(2, 0)
        if not isinstance(result, int):
            raise RemoteError(f"{self.address}: malformed result field")
        resp[2] = _signed64(result)
        return resp

    def open(self) -> None:
        r = self._call(REQ_OPEN, want=RESP_OPENED)
        if r[2] != 0:
            raise RemoteError(f"{self.address}: disc open/authentication failed (result {r[2]})")
        self._open = True

    def size(self) -> int:
        r = self._call(REQ_SIZE, want=RESP_SIZE)
        if r[2] <= 0:
            raise RemoteError(f"{self.address}: no disc size (result {r[2]})")
        return r[2]

    def read(self, lsn: int, count: int) -> bytes:
        """Read exactly `count` sectors from `lsn`, looping on the server's short reads."""
        out = bytearray()
        while count:
            r = self._call(REQ_READ, lsn, count, want=RESP_READ)
            got = r[2]
            data = r.get(3, b"")
            if got <= 0 or len(data) != got * SECTOR_SIZE:
                raise RemoteError(f"{self.address}: read failed at LSN {lsn} (result {got}, {len(data)} bytes)")
            out += data
            lsn += got
            count -= got
        return bytes(out)

    def close(self) -> None:
        try:
            if self._open:
                self._call(REQ_CLOSE, want=RESP_CLOSED)
        except (RemoteError, OSError):
            pass
        finally:
            self._open = False
            self.sock.close()


class RemoteDisc(io.RawIOBase):
    """A disc on a SACD Ripper server as a seekable read-only file of 2048-byte sectors.

    SACDImage, read_toc() and anything else that takes a binary file object can use it.
    Reads are fetched in whole sectors with a small read-ahead cache.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 60.0, readahead: int = MAX_BLOCK):
        self.client = PS3Client(host, port, timeout)
        try:
            self.client.open()
            self.sectors = self.client.size()
        except Exception:
            self.client.close()
            raise
        self._pos = 0
        self._readahead = readahead
        self._cache_lsn = -1
        self._cache = b""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self._pos, os.SEEK_END: self.sectors * SECTOR_SIZE}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def read_sectors(self, lsn: int, count: int) -> bytes:
        return self.client.read(lsn, count)

    def readinto(self, b) -> int:
        end = min(self._pos + len(b), self.sectors * SECTOR_SIZE)
        n = 0
        while self._pos < end:
            lsn, off = divmod(self._pos, SECTOR_SIZE)
            if not (self._cache_lsn <= lsn < self._cache_lsn + len(self._cache) // SECTOR_SIZE):
                count = min(self._readahead, self.sectors - lsn)
                self._cache, self._cache_lsn = self.client.read(lsn, count), lsn
            start = (lsn - self._cache_lsn) * SECTOR_SIZE + off
            chunk = self._cache[start:start + (end - self._pos)]
            b[n:n + len(chunk)] = chunk
            n += len(chunk)
            self._pos += len(chunk)
        return n

    def close(self) -> None:
        if not self.closed:
            self.client.close()
        super().close()


# --- simulator -----------------------------------------------------------------------------

class PS3Simulator:
    """Serves an already-decrypted SACD ISO the way the PS3 SACD Ripper server does.

    Faithful to src/server.c, including its quirks, so clients are tested against the real
    behaviour: one client at a time, reads capped at 512 sectors and cut at area boundaries
    (with the server's block-size bookkeeping carried between requests), the 0-byte message
    terminator, and the connection closing after DISC_CLOSE.

    Test knobs: `rate` (bytes/s, emulates the PS3 drive), `latency` (seconds per request),
    `drop_after` (close the connection after N reads), `fail_lsn` (reads covering this LSN
    return result -1), `no_disc` (DISC_OPEN fails).
    """

    def __init__(self, iso: str | os.PathLike, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *,
                 rate: float = 0, latency: float = 0, drop_after: int = 0, fail_lsn: int | None = None,
                 no_disc: bool = False, log=None):
        self.iso = iso
        self.rate, self.latency, self.drop_after, self.fail_lsn = rate, latency, drop_after, fail_lsn
        self.no_disc = no_disc
        self.log = log or (lambda msg: None)
        self.reads = 0
        sim = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                sim._session(self.request)

        class Server(socketserver.TCPServer):
            allow_reuse_address = True
            request_queue_size = 1      # listen(list_s, 1)

        self._server = Server((host, port), Handler)
        self.address = self._server.server_address
        self._thread: threading.Thread | None = None

    # The PS3 handles one connection at a time on its listener thread; TCPServer does the same.
    def serve_forever(self) -> None:
        self._server.serve_forever()

    def start(self) -> PS3Simulator:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> PS3Simulator:  # noqa: PYI034 (Self needs Python 3.11)
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _session(self, sock: socket.socket) -> None:
        r = _Reader(sock)
        img = None
        enc = [0, 0, 0, 0]              # encrypted_start_1, end_1, start_2, end_2
        block_size = 0                  # carried across requests, as in server.c
        self.log(f"client {sock.getpeername()[0]} connected")
        try:
            while True:
                try:
                    req = decode_message(r)
                except (EOFError, OSError, RemoteError):
                    break
                rtype, offset, count = req.get(1, 0), req.get(2, 0), req.get(3, 0)
                if self.latency:
                    time.sleep(self.latency)
                result, data = -1, None
                if rtype == REQ_READ:
                    resp_type = RESP_READ
                    if img is not None:
                        end_lsn = offset + count
                        if offset < enc[0]:
                            block_size = min(enc[0] - offset, MAX_BLOCK)
                        elif enc[0] <= offset <= enc[1]:
                            block_size = min(enc[1] + 1 - offset, MAX_BLOCK)
                        elif enc[1] < offset < enc[2]:
                            block_size = min(enc[2] - offset, MAX_BLOCK)
                        elif enc[2] <= offset <= enc[3]:
                            block_size = min(enc[3] + 1 - offset, MAX_BLOCK)
                        block_size = min(end_lsn - offset, block_size)
                        n = max(0, min(block_size, img.total_sectors - offset))
                        if self.fail_lsn is not None and offset <= self.fail_lsn < offset + max(n, 1):
                            result = -1
                        elif n > 0:
                            data = img.read_sectors(offset, n)
                            result = n
                        else:
                            result = 0
                        self.reads += 1
                        if self.rate and data:
                            time.sleep(len(data) / self.rate)
                elif rtype == REQ_OPEN:
                    resp_type = RESP_OPENED
                    if not self.no_disc:
                        try:
                            img = SACDImage(self.iso)
                            disc = img.read_toc()
                            areas = disc.areas[:2]  # area[0], area[1] in scarletbook_open order
                            enc = [0, 0, 0, 0]
                            for i, a in enumerate(areas):
                                enc[2 * i], enc[2 * i + 1] = a.track_start, a.track_end
                            result = 0
                        except (OSError, SACDError) as e:
                            self.log(f"cannot open {self.iso}: {e}")
                            img = None
                elif rtype == REQ_CLOSE:
                    resp_type = RESP_CLOSED
                    if img is not None:
                        img.close()
                        img = None
                    result = 0
                elif rtype == REQ_SIZE:
                    resp_type = RESP_SIZE
                    if img is not None:
                        result = img.total_sectors
                else:
                    break
                fields = [(1, resp_type), (2, result)]
                if data:
                    fields.append((3, data))
                sock.sendall(encode_message(fields))
                if rtype == REQ_CLOSE:
                    break
                if self.drop_after and self.reads >= self.drop_after:
                    self.log(f"dropping connection after {self.reads} reads")
                    break
        finally:
            if img is not None:
                img.close()
            sock.close()
            self.log("client disconnected")
