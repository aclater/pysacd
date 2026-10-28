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
import logging
import os
import socket
import socketserver
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO, cast

from . import safeio
from .toc import MASTER_TOC_LEN, SECTOR_SIZE, START_OF_MASTER_TOC, SACDError, SACDImage

DEFAULT_PORT = 2002
MAX_BLOCK = 512                 # MAX_PROCESSING_BLOCK_SIZE in sacd-ripper's scarletbook.h

# Network data is untrusted: bound every length before allocating.
MAX_FIELD = MAX_BLOCK * SECTOR_SIZE + 1024      # largest legitimate field: one 512-sector read
MAX_FIELDS = 16                                 # the protocol's messages have 3 fields
# Largest disc a server may report: 8 GiB, just above a dual-layer SACD (DVD-9, 4,173,824 sectors).
# A larger DISC_SIZE would let a server make the client create an enormous sparse file.
MAX_DISC_SECTORS = 1 << 22

REQ_OPEN, REQ_CLOSE, REQ_READ, REQ_SIZE = 1, 2, 3, 4
RESP_OPENED, RESP_CLOSED, RESP_READ, RESP_SIZE = 1, 2, 3, 4


log = logging.getLogger("pysacd.remote")
_REQ_NAMES = {1: "DISC_OPEN", 2: "DISC_CLOSE", 3: "DISC_READ", 4: "DISC_SIZE"}


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
        # Monotonic time by which the current message must have arrived. The socket timeout
        # bounds each recv(); this stops a server trickling a message a byte at a time.
        self.deadline: float | None = None

    def take(self, n: int) -> bytes:
        while len(self.buf) < n:
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise TimeoutError("response not complete before the deadline")
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
                if val >> 64:       # a 10th byte above 1 overflows int64
                    raise RemoteError("malformed varint")
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
    """(host, port) for a 'host:port' address as accepted by sacd_extract -i, or None if it looks like a path."""
    if os.path.exists(spec) or ":" not in spec or spec.startswith(("/", ".")):
        return None
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        return None
    return host.strip("[]"), int(port)


class PS3Client:
    """Talks to a SACD Ripper server (a PS3, or PS3Simulator)."""

    def __init__(self, host: str, port: int = DEFAULT_PORT, timeout: float = 60.0):
        self.address = f"{host}:{port}"
        self.timeout = timeout
        # Addresses only at debug level: logs get pasted into issues
        log.debug("connecting to %s (timeout %.0f s)", self.address, timeout)
        t0 = time.monotonic()
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            log.error("cannot connect to the server after %.1f s: %s", time.monotonic() - t0, e)
            raise RemoteError(f"cannot connect to {host}:{port}: {e}") from e
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log.info("connected to the server in %.0f ms", (time.monotonic() - t0) * 1e3)
        self.reader = _Reader(self.sock)
        self._open = False
        self.requests = self.bytes_read = 0
        self.busy = 0.0                 # seconds spent waiting for responses
        self._started = time.monotonic()

    def _call(self, req_type: int, offset: int = 0, count: int = 0, want: int = 0, quiet: bool = False) -> dict:
        name = _REQ_NAMES.get(req_type, str(req_type))
        fail = log.debug if quiet else log.error
        t0 = time.monotonic()
        try:
            self.sock.sendall(encode_message([(1, req_type), (2, offset), (3, count)]))
            self.reader.deadline = t0 + self.timeout
            try:
                resp = decode_message(self.reader)
            finally:
                self.reader.deadline = None
        except TimeoutError as e:
            fail("%s(offset=%d, count=%d): no response within %.0f s", name, offset, count, self.timeout)
            raise RemoteError(f"{self.address}: {name} timed out after {self.timeout:.0f} s") from e
        except (EOFError, OSError) as e:
            fail("%s(offset=%d, count=%d) failed after %.0f ms: %r", name, offset, count,
                 (time.monotonic() - t0) * 1e3, e)
            raise RemoteError(f"{self.address}: {name}: {e or type(e).__name__}") from e
        elapsed = time.monotonic() - t0
        self.requests += 1
        self.busy += elapsed
        data = resp.get(3, b"")
        result = resp.get(2, 0)
        log.debug("%s(offset=%d, count=%d) -> type %r result %r, %d data bytes, %.1f ms", name, offset, count,
                  resp.get(1), _signed64(result) if isinstance(result, int) else result,
                  len(data) if isinstance(data, bytes) else 0, elapsed * 1e3)
        if resp.get(1) != want:
            log.error("%s: unexpected response (wanted type %d): %r", name, want,
                      {k: (v if not isinstance(v, bytes) else f"<{len(v)} bytes>") for k, v in resp.items()})
            raise RemoteError(f"{self.address}: unexpected response type {resp.get(1)!r} (wanted {want})")
        if not isinstance(result, int):
            log.error("%s: malformed result field", name)
            raise RemoteError(f"{self.address}: malformed result field")
        if not isinstance(data, bytes):
            log.error("%s: malformed data field", name)
            raise RemoteError(f"{self.address}: malformed data field")
        resp[2] = _signed64(result)
        return resp

    def open(self) -> None:
        r = self._call(REQ_OPEN, want=RESP_OPENED)
        if r[2] != 0:
            log.error("disc open/authentication failed (result %d); is a disc in the drive?", r[2])
            raise RemoteError(f"{self.address}: disc open/authentication failed (result {r[2]})")
        log.info("disc opened")
        self._open = True

    def size(self) -> int:
        r = self._call(REQ_SIZE, want=RESP_SIZE)
        if r[2] <= 0:
            log.error("no disc size (result %d)", r[2])
            raise RemoteError(f"{self.address}: no disc size (result {r[2]})")
        if r[2] > MAX_DISC_SECTORS:
            log.error("disc size %d sectors exceeds the largest SACD (%d)", r[2], MAX_DISC_SECTORS)
            raise RemoteError(f"{self.address}: disc size {r[2]} sectors exceeds the largest SACD "
                              f"({MAX_DISC_SECTORS})")
        log.info("disc has %d sectors (%.2f GB)", r[2], r[2] * SECTOR_SIZE / 1e9)
        return r[2]

    def read_some(self, lsn: int, count: int) -> bytes:
        """One DISC_READ: up to `count` sectors from `lsn` (at least one), as the server returns them."""
        r = self._call(REQ_READ, lsn, count, want=RESP_READ)
        got, data = r[2], r.get(3, b"")
        if not 0 < got <= count or len(data) != got * SECTOR_SIZE:
            log.error("read failed at LSN %d (+%d): result %d, %d data bytes", lsn, count, got, len(data))
            raise RemoteError(f"{self.address}: read failed at LSN {lsn} (result {got}, {len(data)} bytes)")
        self.bytes_read += len(data)
        return data

    def read(self, lsn: int, count: int) -> bytes:
        """Read exactly `count` sectors from `lsn`, looping on the server's short reads."""
        out = bytearray()
        while count:
            r = self._call(REQ_READ, lsn, count, want=RESP_READ)
            got = r[2]
            data = r.get(3, b"")
            # The server must return between 1 and `count` sectors: anything else (including
            # over-returning, which would drive `count` negative) is a protocol error.
            if not 0 < got <= count or len(data) != got * SECTOR_SIZE:
                log.error("read failed at LSN %d (+%d): result %d, %d data bytes", lsn, count, got, len(data))
                raise RemoteError(f"{self.address}: read failed at LSN {lsn} (result {got}, {len(data)} bytes)")
            if got < count:
                log.debug("short read at LSN %d: %d of %d sectors", lsn, got, count)
            self.bytes_read += len(data)
            out += data
            lsn += got
            count -= got
        return bytes(out)

    def close(self) -> None:
        try:
            if self._open:
                self._call(REQ_CLOSE, want=RESP_CLOSED, quiet=True)
        except (RemoteError, OSError) as e:
            # Usually follows an earlier failure that was already logged
            log.info("DISC_CLOSE failed: %s", e)
        finally:
            self._open = False
            self.sock.close()
            span = time.monotonic() - self._started
            log.info("connection closed after %.1f s: %d requests, %.1f MB read, %.1f MB/s while waiting on the "
                     "server", span, self.requests, self.bytes_read / 1e6,
                     self.bytes_read / 1e6 / self.busy if self.busy else 0.0)


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
        # A fresh server session starts with block size 0, and on a single-area disc nothing
        # sets it for reads after the audio area (they return 0 sectors). Reading one sector
        # before the first area primes it, as sacd_extract's own TOC reads do.
        # The master TOC lies before the first area, so reading it primes the block size too,
        # and fetch() compares it on reconnect to make sure it's still the same disc.
        if self.sectors >= START_OF_MASTER_TOC + MASTER_TOC_LEN:
            self.master_toc = self.client.read(START_OF_MASTER_TOC, MASTER_TOC_LEN)
        else:
            self.master_toc = self.client.read_some(0, 1)
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


# --- fetching a disc to a local file -------------------------------------------------------

def needed_ranges(disc, total_sectors: int, areas: list | None) -> list[tuple[int, int]]:
    """LSN ranges [start, end) to fetch so that `areas` (None = whole disc) can be converted locally.

    Everything except the audio of areas that aren't wanted: lead-in, master and area TOCs
    (TOC-1 and TOC-2), and the wanted areas' audio. The rest stays a hole in a sparse file.
    """
    if not 0 < total_sectors <= MAX_DISC_SECTORS:
        log.error("implausible disc size %d sectors", total_sectors)
        raise RemoteError(f"implausible disc size {total_sectors} sectors")
    for a in disc.areas:    # the TOC came over the network: reject areas outside the disc
        if not (0 <= a.toc_start < total_sectors and 0 <= a.track_start <= a.track_end < total_sectors):
            log.error("malformed TOC: %s area at LSN %d..%d (TOC at %d) outside a disc of %d sectors",
                      a.kind, a.track_start, a.track_end, a.toc_start, total_sectors)
            raise RemoteError(f"malformed TOC: {a.kind} area at LSN {a.track_start}..{a.track_end} "
                              f"(TOC at {a.toc_start}) lies outside a disc of {total_sectors} sectors")
    if areas is None:
        return [(0, total_sectors)]
    skip = sorted((a.track_start, a.track_end + 1) for a in disc.areas if a not in areas)
    out, pos = [], 0
    for s, e in skip:
        if s > pos:
            out.append((pos, s))
        pos = max(pos, e)
    if pos < total_sectors:
        out.append((pos, total_sectors))
    return out


def fetch(host: str, port: int, path: str | os.PathLike, ranges: list[tuple[int, int]] | None = None,
          retries: int = 3, progress=None, chunk: int = MAX_BLOCK * 4, backoff: float = 1.0) -> int:
    """Copy sectors from a SACD Ripper server into a local plain (2048-byte) ISO file.

    `ranges` defaults to the whole disc; sectors outside them are left as holes (zeros).
    A dropped connection is reopened and the copy resumes where it stopped, up to `retries`
    times in a row, waiting backoff * 2**(n-1) seconds (at most 10) before the n-th retry.
    `progress(done, total)` is called after each reply (in sectors).
    Returns the disc size in sectors.
    """
    disc = RemoteDisc(host, port)
    total = disc.sectors
    # Size plus the master TOC: another pressing of the same album can share the size.
    ident = (total, disc.master_toc)
    asked = ranges or [(0, total)]
    ranges = [(max(0, s), min(e, total)) for s, e in asked if min(e, total) > max(0, s)]
    if ranges != asked:
        log.warning("fetch ranges clamped to the disc's %d sectors: %r -> %r", total, asked, ranges)
    todo = sum(e - s for s, e in ranges)
    done = failures = 0
    tmp = safeio.temp_name(path)    # written under a new, unpredictable name; moved into place at the end
    try:
        with os.fdopen(safeio.create(tmp), "wb") as out:
            out.truncate(total * SECTOR_SIZE)
            for start, end in ranges:
                lsn = start
                while lsn < end:
                    n = min(chunk, end - lsn)
                    try:
                        data = disc.client.read_some(lsn, n)     # write each reply as it arrives
                        n = len(data) // SECTOR_SIZE
                    except (RemoteError, OSError):
                        failures += 1
                        if failures > retries:
                            raise
                        disc.close()
                        time.sleep(min(backoff * 2 ** (failures - 1), 10))
                        disc = RemoteDisc(host, port)
                        if (disc.sectors, disc.master_toc) != ident:     # a different disc: don't splice it into this one
                            log.error("the disc changed on reconnect (size %d -> %d sectors, or a different "
                                      "master TOC)", total, disc.sectors)
                            raise RemoteError(f"{host}:{port}: the disc changed during the copy")
                        continue
                    failures = 0
                    out.seek(lsn * SECTOR_SIZE)
                    out.write(data)
                    lsn += n
                    done += n
                    if progress:
                        progress(done, todo)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        disc.close()
    return total


def read_remote_toc(host: str, port: int):
    """(Disc, total sectors) of the disc on a SACD Ripper server."""
    rd = RemoteDisc(host, port)
    try:
        return SACDImage(cast(BinaryIO, rd)).read_toc(), rd.sectors
    finally:
        rd.close()


def fetch_temp(host: str, port: int, workdir: str | os.PathLike, want_area, progress=None):
    """Fetch what's needed to convert the areas selected by `want_area(area)` into a temporary
    sparse ISO in `workdir`. Returns (path, Disc); the caller deletes the file."""
    disc, total = read_remote_toc(host, port)
    areas = [a for a in disc.areas if want_area(a)]
    os.makedirs(workdir, exist_ok=True)
    tmp = str(safeio.temp_name(Path(workdir) / "remote.iso"))
    fetch(host, port, tmp, needed_ranges(disc, total, areas), progress=progress)    # removes it on failure
    return tmp, disc


class RateMeter:
    """Formats 'n%  x MB/s' progress lines for fetch()."""

    def __init__(self, write, every: float = 1.0):
        self.write, self.every, self.t0, self.last = write, every, time.time(), 0.0

    def __call__(self, done: int, total: int) -> None:
        now = time.time()
        if now - self.last < self.every and done < total:
            return
        self.last = now
        rate = done * SECTOR_SIZE / max(now - self.t0, 1e-6) / 1e6
        self.write(f"\r  network copy: {100 * done / max(total, 1):5.1f}%  {done * SECTOR_SIZE / 1e6:8.0f} MB  "
                   f"{rate:6.1f} MB/s")
        if done >= total:
            self.write("\n")


# --- simulator -----------------------------------------------------------------------------

class PS3Simulator:
    """Serves an already-decrypted SACD ISO the way the PS3 SACD Ripper server does.

    Faithful to src/server.c, including its quirks, so clients are tested against the real
    behaviour: one client at a time, reads capped at 512 sectors and cut at area boundaries
    (with the server's block-size bookkeeping carried between requests), the 0-byte message
    terminator, and the connection closing after DISC_CLOSE.

    Test knobs: `rate` (bytes/s, emulates the PS3 drive), `latency` (seconds per request),
    `drop_after` (close each connection after N reads), `fail_lsn` (reads covering this LSN
    return result -1), `no_disc` (DISC_OPEN fails), `over_return` (send N sectors more than
    requested, to test that clients reject a misbehaving server), `disc_size` (report this
    DISC_SIZE instead of the real one).

    Exposure: it listens on 127.0.0.1 by default. A connection that sends nothing for
    `idle_timeout` seconds is closed, so one idle client can't block the server for ever.
    """

    def __init__(self, iso: str | os.PathLike, host: str = "127.0.0.1", port: int = DEFAULT_PORT, *,
                 rate: float = 0, latency: float = 0, drop_after: int = 0, fail_lsn: int | None = None,
                 no_disc: bool = False, over_return: int = 0, disc_size: int | None = None,
                 idle_timeout: float = 60.0, log=None):
        self.iso = iso
        self.rate, self.latency, self.drop_after, self.fail_lsn = rate, latency, drop_after, fail_lsn
        self.no_disc = no_disc
        self.over_return = over_return
        self.disc_size = disc_size
        self.idle_timeout = idle_timeout
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
        session_reads = 0
        if self.idle_timeout:
            sock.settimeout(self.idle_timeout)
        self.log(f"client {sock.getpeername()[0]} connected")
        try:
            while True:
                try:
                    req = decode_message(r)
                except (EOFError, OSError, RemoteError):
                    break
                rtype, offset, count = req.get(1, 0), req.get(2, 0), req.get(3, 0)
                if not (isinstance(rtype, int) and isinstance(offset, int) and isinstance(count, int)):
                    self.log("malformed request, closing the connection")
                    break
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
                        if self.over_return:
                            n = min(n + self.over_return, img.total_sectors - offset)
                        if self.fail_lsn is not None and offset <= self.fail_lsn < offset + max(n, 1):
                            result = -1
                        elif n > 0:
                            data = img.read_sectors(offset, n)
                            result = n
                        else:
                            result = 0
                        self.reads += 1
                        session_reads += 1
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
                        result = self.disc_size if self.disc_size is not None else img.total_sectors
                else:
                    break
                fields = [(1, resp_type), (2, result)]
                if data:
                    fields.append((3, data))
                sock.sendall(encode_message(fields))
                if rtype == REQ_CLOSE:
                    break
                if self.drop_after and session_reads >= self.drop_after:
                    self.log(f"dropping connection after {session_reads} reads")
                    break
        finally:
            if img is not None:
                img.close()
            sock.close()
            self.log("client disconnected")
