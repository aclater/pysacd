# SACD Ripper network protocol (PS3 server)

`sacd_extract -i 192.0.2.10:2002` reads a disc from a PS3 running the SACD Ripper
homebrew. This document describes what goes over the wire, as implemented in
sacd-ripper's `libs/libsacd/sacd_ripper.proto`, `libs/libsacd/sacd_input.c` (client)
and `src/server.c` (PS3 server), and as reproduced by `pysacd.remote`.

The PS3 reads the disc in its Blu-ray drive and decrypts the audio areas itself.
Only plain 2048-byte logical sectors cross the network, so a server that serves an
already-decrypted ISO is indistinguishable from a PS3 to the client.

## Transport

- TCP, port 2002. The PS3 listens with a backlog of 1 and serves one client at a time.
- Messages are protobuf (proto2, nanopb) with **no length prefix**. Each message is
  followed by a single `0x00` byte: nanopb stops decoding at a zero tag, so the zero
  byte marks the end of the message. Both directions use this framing.
- The client decodes responses incrementally, receiving more bytes until a complete
  message parses.

## Messages

```proto
message ServerRequest {
  enum Type { DISC_OPEN = 1; DISC_CLOSE = 2; DISC_READ = 3; DISC_SIZE = 4; }
  required Type   type          = 1;
  required uint32 sector_offset = 2 [default = 0];
  required uint32 sector_count  = 3 [default = 0];
}

message ServerResponse {
  enum Type { DISC_OPENED = 1; DISC_CLOSED = 2; DISC_READ = 3; DISC_SIZE = 4; }
  required Type  type   = 1;
  required int64 result = 2;           // negative values are 10-byte varints
  optional bytes data   = 3;           // at most 512 * 2048 bytes
}
```

| request | response `result` | notes |
|---|---|---|
| `DISC_OPEN` | 0 on success | opens `/dev_bdvd`, reads the TOC and authenticates the disc; -1 if there is no disc |
| `DISC_SIZE` | total sectors | -1 if no disc is open |
| `DISC_READ` | sectors returned | `data` holds `result * 2048` bytes; see below |
| `DISC_CLOSE` | 0 | the server then closes the connection |

## Reads

A `DISC_READ` returns **at most 512 sectors** (`MAX_PROCESSING_BLOCK_SIZE`) and may return
fewer than requested. The server decrypts each audio area separately, so it cuts reads
at the area boundaries. With `[s1, e1]` the first area's audio LSN range (`track_start`,
`track_end`, inclusive) and `[s2, e2]` the second's:

| request offset | block size |
|---|---|
| `< s1` | `min(s1 - offset, 512)` |
| `s1 .. e1` | `min(e1 + 1 - offset, 512)` |
| `e1 < offset < s2` | `min(s2 - offset, 512)` |
| `s2 .. e2` | `min(e2 + 1 - offset, 512)` |

then `min(count, block size)`. On a single-area disc `s2 = e2 = 0`, so
none of the rows match after the first area and the server reuses the block size from the
previous request. Clients must loop until they have all the sectors they asked for.

The first time an encrypted range is read, the server checks whether the disc is
encrypted at all (some DSD 3-in-14/16 discs aren't) by looking at bytes 16-23 of the
first block. That only affects the PS3's own decryption, not the bytes sent.

## Testing without a PS3

```
pysacd ps3sim disc.iso                       # listen on 0.0.0.0:2002
sacd_extract -I -i 127.0.0.1:2002            # copy the disc back as an ISO
```

`pysacd.remote.PS3Simulator` follows `src/server.c`, including the boundary cuts and the
carried-over block size, and adds knobs for testing clients: `--rate` (MB/s, to emulate
the PS3 drive), `--latency` (ms per request), `--drop-after N` (close the connection
after N reads), `--fail-lsn L` (reads covering LSN L return -1) and `--no-disc`.

Checked against sacd-ripper's `sacd_extract` (built from source, 2026-10-28):

- `sacd_extract -P` prints the same disc information as with the local ISO.
- `sacd_extract -I` through the simulator produced an ISO byte-identical to the source
  (a 3.39 GB plain-DSD disc).
- `sacd_extract -2 -m -s` on a DST stereo + 5.1 disc produced 28 DSF files identical to
  a local extraction of the same ISO.

What the simulator can't reproduce: the PS3 drive's real speed and error behaviour, and
its guess about whether a disc is encrypted. A final check against a real PS3 is still
worthwhile.

## Client

`pysacd.remote.RemoteDisc(host, port)` opens the disc and behaves as a seekable read-only
file of 2048-byte sectors, so `SACDImage`, `read_toc()` and anything else that takes a
binary file object work unchanged. `PS3Client` is the lower-level request/response API.
