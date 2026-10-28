# DST frame format and decoding process

## 1. Scope and provenance

This document specifies the frame syntax and the decoding process of DST
(Direct Stream Transfer), the lossless coding of 1-bit oversampled audio
defined in MPEG-4 Audio (ISO/IEC 14496-3) and used on Super Audio CD.

It was written for a clean-room implementation. It was produced from a study of
the behaviour of the ISO/MPEG-4 reference decoder (the Philips reference module
for DST). That module's licence is not GPL-compatible, so this document contains
no code from it. It describes only the bitstream format and the arithmetic of
the decoding process. All element names here are descriptive names. They are
not the reference implementation's identifiers.

The document also compares that behaviour with pysacd's current decoder,
`csrc/dstdec.c`, which is a port of FFmpeg's DST decoder.

**Sample rate.** The case that matters for SACD is DSD64, where the sample rate
is 64 x 44100 Hz. In this document `fs44` is the sample rate divided by 44100,
so `fs44 = 64` for DSD64. A frame always covers 1/75 s. The following quantities
depend on `fs44`:

| Quantity | Formula | DSD64 (fs44=64) | fs44=128 | fs44=256 |
|---|---|---|---|---|
| Bits per channel per frame, `NB` | 588 x fs44 | 37632 | 75264 | 150528 |
| Bytes per channel per frame, `FL` | 588 x fs44 / 8 | 4704 | 9408 | 18816 |
| Width of the filter-segment resolution field | bitlen(FL - 128) | 13 | 14 | 15 |
| Width of the ptable-segment resolution field | bitlen(FL - 4) | 13 | 14 | 15 |
| Width of the segment-length fields | depends on FL (see 2.4) | | | |

Nothing else depends on `fs44`. The reference decoder sizes its per-bit working
arrays for fs44 = 64 only, and its driver always initialises it with 64. In
practice, therefore, it supports only DSD64. The format itself is parametric in
`fs44`.

The decoder takes two external parameters that are not in the DST frame. They
come from the container (the SACD area TOC and the sector/packet layer):

* `Nch`, the number of channels (1 to 6).
* The **frame length in bytes**, `FB`, of each DST frame. The total number of
  frame bits is `8 x FB`.

**Notation used throughout.**

* `bitlen(x)` is the number of bits needed to write the non-negative integer x
  in binary: the smallest y >= 0 with 2^y > x. So bitlen(0) = 0, bitlen(1) = 1,
  bitlen(2) = bitlen(3) = 2, bitlen(4..7) = 3, bitlen(4576) = 13. A field of
  width 0 occupies no bits and has the value 0.
* `floor` is mathematical floor. All divisions in this document are exact
  rational divisions followed by the stated rounding.
* `uimsbf(n)` is an n-bit unsigned integer, most significant bit first.
  `tcimsbf(n)` is an n-bit two's-complement signed integer, MSB first. Its
  range is -2^(n-1) to 2^(n-1)-1.
* `bslbf(1)` is a single bit.

## 2. Frame syntax

### 2.1 Bit order

A DST frame is a sequence of `FB` bytes. Bits are read starting at the most
significant bit of byte 0, then towards the LSB, then into byte 1, and so on.
Multi-bit fields are MSB first and are not byte-aligned. Fields follow each
other with no padding, except where stated.

### 2.2 Top level

| Element | Bits | Condition / loop | Meaning |
|---|---|---|---|
| `DST_coded` | 1 | always | 1 = DST-coded frame; 0 = plain DSD frame |
| *if DST_coded = 0:* | | | |
| `DST_X_bit_plain` | 1 | | Reserved/ancillary bit. Its value is ignored. |
| `stuffing` | 6 | | Must be 0b000000. |
| `DSD_data` | 8 x FL x Nch | | Raw DSD, byte-interleaved (see 3.10) |
| *if DST_coded = 1:* | | | |
| `segmentation()` | var | | 2.3 |
| `mapping()` | var | | 2.5 |
| `half_probability()` | Nch | | 2.6 |
| `filter_coef_sets()` | var | | 2.7 |
| `probability_tables()` | var | | 2.8 |
| `arithmetic_coded_data()` | all remaining bits | | 2.9 |

In the plain branch the header is exactly one byte, so the DSD payload is
byte-aligned. The reference reads exactly `FL x Nch` payload bytes.

* If the frame is longer than `1 + FL x Nch` bytes, the reference ignores the
  extra bytes.
* If the frame is shorter, the missing payload bytes come out as zero. The
  reference does this with an out-of-bounds read whose result is discarded. An
  implementation should zero-fill explicitly.

### 2.3 segmentation()

| Element | Bits | Condition | Meaning |
|---|---|---|---|
| `same_segmentation` | 1 | always | 1 = the probability-table segmentation equals the filter segmentation and is not transmitted |
| `table_segmentation(FILTER)` | var | always | 2.4, with Smax = 4, Lmin = 1024 |
| `table_segmentation(PTABLE)` | var | if same_segmentation = 0 | 2.4, with Smax = 8, Lmin = 32 |

If `same_segmentation = 1`, the probability-table segmentation is a copy of the
filter segmentation: the same resolution, the same number of segments per
channel, and the same segment lengths.

### 2.4 table_segmentation(kind)

Parameters for each kind:

| kind | Smax (nominal max segments per channel) | Lmin (min segment length, bits) | Lmin/8 (bytes) |
|---|---|---|---|
| FILTER | 4 | 1024 | 128 |
| PTABLE | 8 | 32 | 4 |

The decoder maintains these state variables for each segmentation:

* `R`, the segment resolution in bytes. It is transmitted at most once per
  segmentation (once for filters, once for ptables) and shared by all channels.
* `remBytes`, the bytes still available for explicit segment lengths in the
  current channel. Its initial value is `FL - Lmin/8`, which is 4576 for
  filters and 4700 for ptables at DSD64.
* `defBits`, the number of bits already assigned to explicit segments in the
  current channel. Its initial value is 0.

**Syntax.**

| Element | Bits | Condition / loop | Meaning |
|---|---|---|---|
| `same_segm_all_ch` | 1 | always | 1 = one segmentation is sent and used for every channel |
| **if same_segm_all_ch = 1:** | | | |
| `end_of_channel_segm` | 1 | first | 1 = no (more) explicit segments |
| *repeat while the last end_of_channel_segm read was 0:* | | | |
| `segment_resolution` | bitlen(FL - Lmin/8) | only before the first explicit segment of this segmentation | R, in bytes |
| `segment_length` | bitlen(floor(remBytes / R)) | | length in units of R bytes |
| `end_of_channel_segm` | 1 | | 1 = this was the last explicit segment |
| **if same_segm_all_ch = 0:** | | | |
| *for each channel c = 0 .. Nch-1:* | | | |
| *repeat:* | | | |
| `end_of_channel_segm` | 1 | | 1 = the channel's segment list ends here |
| `segment_resolution` | bitlen(FL - Lmin/8) | if end_of_channel_segm = 0 and R not yet read for this kind | R, in bytes |
| `segment_length` | bitlen(floor(remBytes / R)) | if end_of_channel_segm = 0 | length in units of R bytes |
| *until end_of_channel_segm = 1; then move to the next channel and reset remBytes and defBits (R is kept)* | | | |

**Semantics.**

* Each explicit `segment_length` value `L` gives a segment of `8 x R x L` bits.
  After each one, `defBits` increases by `8 x R x L` and `remBytes` decreases
  by `R x L`. The field width for the next length is recomputed from the new
  `remBytes`.
* A channel with `k` explicit lengths has `k + 1` segments. The last segment is
  implicit: it runs from the end of the explicit segments to the end of the
  frame, `NB` bits. The reference represents it as a stored length of 0.
* With `same_segm_all_ch = 1`, channel 0's segment list is copied to every
  channel.
* If no explicit segment exists in this segmentation, R is not transmitted. It
  is then irrelevant, and the reference sets it to 1.
* With `same_segmentation = 1`, the probability tables use the filter
  segmentation unchanged, R included.

**Validity, checked as each field is read.**

* R must satisfy `1 <= R <= FL - Lmin/8`.
* Every explicit segment must satisfy
  `Lmin <= 8 x R x L <= 8 x FL - defBits - Lmin`, where `defBits` excludes the
  current segment. This is the same as saying `L >= 1` and `R x L <= remBytes`.
  As a result, every segment, the implicit last one included, is at least
  `Lmin` bits long, and `L = 0` is invalid.
* Segment count. The reference's limit is applied differently in the two
  branches. This is a quirk:
  * `same_segm_all_ch = 1`: after an `end_of_channel_segm = 0` the decoder
    checks that fewer than Smax explicit lengths have been read so far, and
    errors otherwise. Up to **Smax explicit lengths** are therefore accepted,
    giving up to **Smax + 1 segments** (5 for filters, 9 for ptables). For
    ptables, 9 segments overflows the reference's own arrays, which hold 8.
  * `same_segm_all_ch = 0`: before each `end_of_channel_segm` is read, the
    decoder checks that fewer than Smax explicit lengths have been read for
    the channel. Up to **Smax - 1 explicit lengths** are accepted, giving up
    to **Smax segments** (4 for filters, 8 for ptables). If Smax lengths have
    been read, the decoder fails before reading the next
    `end_of_channel_segm`, even if that bit would have been 1.
  * Recommendation. A decoder should accept what the reference accepts, sizing
    arrays for Smax + 1 segments. An encoder must never emit more than Smax
    segments per channel. See section 8.
* When the ptable segmentation is copied (`same_segmentation = 1`), the
  reference also checks two things: at most 8 segments per channel, and each
  explicit segment at least 32 bits. Neither check can fail, because filter
  segments are at least 1024 bits and number at most 5.
* The reference also derives a "same segmentation for all channels" indicator
  for the copied ptable segmentation. Nothing uses it.

**Bit position of segments.** For channel c, let segment j have explicit length
`L_j` for j < n-1, where n is the number of segments. Let `S_0 = 0` and
`S_(j+1) = S_j + 8 x R x L_j`. Then segment j covers bit indices
`[S_j, S_(j+1))` for j < n-1, and the last segment covers `[S_(n-1), NB)`.

### 2.5 mapping()

| Element | Bits | Condition | Meaning |
|---|---|---|---|
| `same_mapping` | 1 | always | 1 = the ptable mapping equals the filter mapping and is not transmitted |
| `table_mapping(FILTER)` | var | always | Assigns a filter number to each filter segment. Uses the filter segmentation. |
| `table_mapping(PTABLE)` | var | if same_mapping = 0 | Assigns a ptable number to each ptable segment. Uses the ptable segmentation. |

**table_mapping(kind).** `T` is the running number of distinct tables so far.
It starts at 1, because table 0 is implicitly assigned to segment 0 of
channel 0. `nseg[c]` is the number of segments of channel c in this kind's
segmentation.

| Element | Bits | Condition / loop | Meaning |
|---|---|---|---|
| `same_map_all_ch` | 1 | always | 1 = one list of table numbers is sent and used for all channels |
| **if same_map_all_ch = 1:** | | | |
| *for j = 1 .. nseg[0]-1:* | | | |
| `table_index` | bitlen(T) | | table number for segment j (all channels) |
| **if same_map_all_ch = 0:** | | | |
| *for c = 0 .. Nch-1, for j = 0 .. nseg[c]-1, skipping (c=0, j=0):* | | | |
| `table_index` | bitlen(T) | | table number for segment j of channel c |

**Semantics and validity.**

* Each `table_index` value `v` must satisfy `v <= T`.
  * If `v = T`, the segment opens a new table and T increases by 1.
  * If `v > T`, the frame is invalid.
* Tables are therefore numbered in order of first appearance, scanning
  channel 0's segments, then channel 1's, and so on.
* The field width is recomputed before every index from the current T:

  | T | width |
  |---|---|
  | 1 | 1 |
  | 2, 3 | 2 |
  | 4 to 7 | 3 |
  | 8 to 15 | 4 |
* With `same_map_all_ch = 1`, every channel must have the same number of
  segments as channel 0, otherwise the frame is invalid ("mapping can't be the
  same for all channels"). Channel 0's table numbers are then copied to every
  channel. This check runs after channel 0's indices have been read.
* With `same_map_all_ch = 1` and `nseg[0] = 1`, the mapping consists of the
  flag bit alone, and all channels use table 0.
* After the mapping is read, the final T is the number of tables of this kind:
  `NF` for filters, `NP` for ptables.
  * The frame is invalid if `T > 2 x Nch`.
  * This check is applied only once, at the end of the mapping.

**Copying the mapping (`same_mapping = 1`).**

* For every channel c, the ptable segmentation and the filter segmentation must
  have the same number of segments, otherwise the frame is invalid (segment
  number mismatch). The segment lengths may differ: `same_segmentation = 0`
  combined with `same_mapping = 1` is legal.
* The ptable number of segment j of channel c is the filter number of segment j
  of channel c.
* `NP = NF`. This is then checked against `2 x Nch` again, which cannot fail.

### 2.6 half_probability()

| Element | Bits | Condition / loop | Meaning |
|---|---|---|---|
| `half_prob[c]` | 1 | for c = 0 .. Nch-1 | 1 = the first bits of channel c are coded with probability 1/2 (see 3.6) |

### 2.7 filter_coef_sets()

The decoder reads one coefficient set for each filter number f = 0 .. NF-1, in
order.

| Element | Bits | Condition / loop | Meaning |
|---|---|---|---|
| `coded_pred_order` | uimsbf(7) | | Prediction order `N_f = coded_pred_order + 1`, range 1 to 128 |
| `coef_coded` | 1 | | 0 = coefficients sent verbatim; 1 = entropy coded |
| **if coef_coded = 0:** | | | |
| `coef[i]` | tcimsbf(9) | for i = 0 .. N_f-1 | coefficient a_i, range -256 to 255 |
| **if coef_coded = 1:** | | | |
| `coding_method` | uimsbf(2) | | method k, 0 to 2. Value 3 is invalid. |
| `coef[i]` | tcimsbf(9) | for i = 0 .. K-1, where K = k+1 | the first K coefficients, verbatim |
| `rice_param` | uimsbf(3) | | Rice parameter m, 0 to 7 |
| `coef_residual[i]` | Rice code (2.10) | for i = K .. N_f-1 | prediction residual r_i |

**Reconstruction of entropy-coded coefficients.** For i = K .. N_f-1, in
increasing order, using the already reconstructed coefficients:

    x_i = sum over t = 0 .. K-1 of  b[k][t] x a_(i-1-t)
    a_i = r_i - floor((x_i + 4) / 8)

Here `floor((x+4)/8)` is an arithmetic right shift by 3 of `x + 4`. The
reference writes it as two cases: `(x+4)/8` for x >= 0, and `-((−x+3)/8)` with
truncating division for x < 0. The two cases are identical to the single floor
expression for every integer x.

Prediction coefficients for filter sets, `b[k][t]`:

| method k | K | t=0 | t=1 | t=2 |
|---|---|---|---|---|
| 0 | 1 | -8 | | |
| 1 | 2 | -16 | 8 | |
| 2 | 3 | -9 | -5 | 6 |

**Validity.**

* The frame is invalid if `K >= N_f`, that is, if the method's order is not
  strictly less than the prediction order. So a filter with N_f = 1 must be
  sent verbatim.
* Method 3 must be rejected. The reference does not test for it explicitly but
  indexes past its 3-entry method tables; this is undefined behaviour.
* Every reconstructed `a_i` with i >= K must lie in -256 to 255, otherwise the
  frame is invalid ("filter coefficient out of range"). Verbatim coefficients
  are in range by construction.
* `rice_param` is not range-checked. The reference's constants suggest a
  nominal maximum of 6 for filters, but the decoder accepts 0 to 7.
* Coefficients a_i with i >= N_f are treated as 0.

After all filters are read, the number of half-probability bits for each
channel is fixed:

    NH[c] = N_(F(c, 0))

where `F(c, 0)` is the filter number of channel c's **first** filter segment.

### 2.8 probability_tables()

The decoder reads one table for each ptable number p = 0 .. NP-1, in order.

| Element | Bits | Condition / loop | Meaning |
|---|---|---|---|
| `coded_ptable_len` | uimsbf(6) | | Table length `M_p = coded_ptable_len + 1`, range 1 to 64 |
| **if M_p = 1:** | — | | **Nothing more is read.** The single entry is P_p[0] = 128. |
| **if M_p > 1:** | | | |
| `ptable_coded` | 1 | | 0 = entries sent verbatim; 1 = entropy coded |
| *if ptable_coded = 0:* | | | |
| `entry[i]` | uimsbf(7) | for i = 0 .. M_p-1 | P_p[i] = value + 1, range 1 to 128 |
| *if ptable_coded = 1:* | | | |
| `coding_method` | uimsbf(2) | | method k, 0 to 2. Value 3 is invalid. |
| `entry[i]` | uimsbf(7) | for i = 0 .. K-1, where K = k+1 | P_p[i] = value + 1 |
| `rice_param` | uimsbf(3) | | m |
| `entry_residual[i]` | Rice code (2.10) | for i = K .. M_p-1 | residual r_i |

**Reconstruction of entropy-coded entries.** For i = K .. M_p-1 the rule is
the same as for filters, applied to the stored entries (the +1 already
applied):

    x_i = sum over t = 0 .. K-1 of  c[k][t] x P_p[i-1-t]
    P_p[i] = r_i - floor((x_i + 4) / 8)

Prediction coefficients for probability tables, `c[k][t]`:

| method k | K | t=0 | t=1 | t=2 |
|---|---|---|---|---|
| 0 | 1 | -8 | | |
| 1 | 2 | -16 | 8 | |
| 2 | 3 | -24 | 24 | -8 |

**Validity.**

* The frame is invalid if `K >= M_p`. Method 3 must be rejected (same quirk as
  for filters).
* Every reconstructed entry with i >= K must lie in 1 to 128, otherwise the
  frame is invalid ("Ptable entry out of range").
* `rice_param` is not range-checked. The nominal maximum is 4.

Note the special case `M_p = 1`: no `ptable_coded` bit and no entry is
transmitted. This differs from the filter case, where `coef_coded` is always
present.

### 2.9 arithmetic_coded_data()

Every bit remaining after the probability tables, up to the end of the frame,
belongs to the arithmetic code. Let `fs = 8 x FB - (number of bits consumed so
far)`, and number the code bits `cb[0] .. cb[fs-1]` in stream order.

| Element | Bits | Condition | Meaning |
|---|---|---|---|
| `cb[0]` | 1 | if fs > 0 | Must be 0, otherwise the frame is invalid ("illegal arithmetic code") |
| `cb[1 .. fs-1]` | fs-1 | | Arithmetic codeword (3.7) |

The **DST_X_bit** of a coded frame is not a raw field. It is the first symbol
of the arithmetic code (3.8).

### 2.10 Rice code (used for coef_residual and entry_residual)

A Rice-coded signed integer with parameter m is read as follows:

1. **Unary run.** Count the `0` bits until a `1` bit is read. The run length
   `q` is the number of zeros; the terminating `1` is consumed.
2. **LSBs.** Read `m` bits as uimsbf(m), giving `lsb`. Nothing is read if
   m = 0.
3. **Magnitude.** `u = q x 2^m + lsb`.
4. **Sign.** If u != 0, read one more bit. A 1 means the value is -u, a 0 means
   +u. If u = 0, no sign bit is read, and the value is 0.

The reference does not bound the unary run. If a Rice code runs past the end of
the frame, its behaviour is undefined: it ignores the end-of-data condition and
can loop. An implementation must treat running out of frame data inside a Rice
code as an error.

## 3. Decoding process (DST_coded = 1)

### 3.1 Per-bit table assignment

For each channel c and each bit index n (0 <= n < NB), define:

* `F(c, n)`, the filter number. It is the table number mapped to the filter
  segment of channel c that contains n.
* `P(c, n)`, the ptable number. It is the table number mapped to the ptable
  segment of channel c that contains n.

Segment boundaries come from section 2.4, and table numbers from section 2.5.
The filter segmentation and the ptable segmentation are independent unless
`same_segmentation = 1`, in which case they have identical boundaries. Even
then the ptable mapping may differ from the filter mapping if
`same_mapping = 0`.

With `same_segm_all_ch = 1`, all channels share the boundaries but not
necessarily the table numbers. Table numbers are also shared only if
`same_map_all_ch = 1`.

### 3.2 Order of processing

The arithmetic decoder is a single stream shared by all channels. Symbols are
decoded in this order:

1. The DST_X_bit (3.8).
2. For n = 0, 1, ..., NB-1: for c = 0, 1, ..., Nch-1, one symbol for bit n of
   channel c.

So for each bit index, all channels are decoded before moving to the next bit
index. Each channel has its own prediction history.

### 3.3 Prediction history

Each channel c keeps a history of its 128 most recently decoded output bits.
Write `h_c[i]` for i = 0 .. 127, where `h_c[0]` is the most recent bit.

**Initial state.** At the start of every frame, independent of the previous
frame, the history is set to `h_c[i] = 1` for odd i and `h_c[i] = 0` for even
i. In stream terms the bit just before the frame is taken as 0, the one before
that as 1, then 0, and so on.

This equals a 16-byte register filled with the byte 0xAA, where byte `j` bit
`t` (LSB = bit 0) holds `h_c[8j + t]`. FFmpeg and the reference both rely on a
little-endian memory view of that register. A portable implementation should
model the history as a logical 128-bit shift register.

**Update.** After the output bit `y` for (c, n) is decided, shift the history
by one: every `h_c[i]` moves to `h_c[i+1]`, the oldest bit `h_c[127]` is
dropped, and `h_c[0] = y`.

The history continues across segment boundaries. A change of filter in the
middle of a frame does not reset it.

### 3.4 Prediction

With f = F(c, n), N = N_f, and a_i the coefficients of filter f:

    S = sum over i = 0 .. N-1 of  a_i x (2 x h_c[i] - 1)

So a history bit of 1 contributes +a_i, and a history bit of 0 contributes
-a_i.

S is then reduced to a 16-bit two's-complement value:

    Spred = ((S + 32768) mod 65536) - 32768

The exact range of S is -32768 to +32768. The reduction therefore changes only
S = +32768, which becomes -32768. That case arises when all 128 coefficients
are -256 and all history bits are 0.

The **sign indicator** is `s = 1` if Spred < 0, and `s = 0` otherwise.

### 3.5 Probability selection

Use p = 128 if both of these hold:

* `half_prob[c] = 1`, and
* `n < NH[c]`.

Otherwise:

    j = min( floor(|Spred| / 8),  M_q - 1 ),    where q = P(c, n)
    p = P_q[j]

Notes:

* `|Spred|` is computed in ordinary integers, so |-32768| = 32768, and j is
  then clamped.
* The shift by 3 comes from the 9-bit coefficient width minus the 6-bit table
  index width.
* The probability p is an integer in 1 to 128, on a scale of 256.

### 3.6 Half-probability rule

`NH[c]` is the prediction order of the filter assigned to channel c's **first**
filter segment. It does not depend on the filter in force at bit n. Filter
segments are at least 1024 bits long and orders are at most 128, so the
half-probability region always lies inside the first filter segment. It may
span several ptable segments, and it overrides them all.

The prediction is still computed for these bits, and the history is still
updated. Only the probability changes.

### 3.7 Arithmetic decoder

**State.**

* `A`, the interval width: a 12-bit unsigned value.
* `C`, the code offset: a 12-bit unsigned value.
* `ptr`, the index of the next code bit.

Code bits at positions `>= fs` are read as 0.

**Initialisation.** Set A = 4095 (2^12 - 1) and C to the 12 bits
`cb[1] .. cb[12]` read as an MSB-first integer, then set `ptr = 13`. Bit
`cb[0]` is not part of C; it must be 0 (2.9).

**Decoding one symbol with probability p (1 to 128):**

1. `q = floor(A / 256)`, with its least significant bit forced to 1 if bit 7
   of A is set:

       q = floor(A / 256) OR (floor(A / 128) AND 1)

2. `w0 = q x p`. This is the width of the sub-interval of symbol 0.
3. `w1 = A - w0`. This is the width of the sub-interval of symbol 1. The
   symbol-1 sub-interval is the lower part, and the symbol-0 sub-interval is
   the upper part.
4. If `C >= w1`: the symbol is `e = 0`, and `C = C - w1`, `A = w0`.
   Otherwise the symbol is `e = 1`, and `A = w1`; C is unchanged.
5. **Renormalisation.** While `A < 2048`: double A, set `C = 2C + cb[ptr]`
   (0 if ptr >= fs), and increase ptr by 1.

   This is equivalent to shifting both by `11 - floor(log2 A)` at once and
   reading that many bits.

Bounds: A >= 2048 before every symbol, so q is between 8 and 15, w0 is between
8 and 1920, and w1 >= 128. Both sub-intervals are non-empty.

The table value p is therefore the scaled probability of the **symbol 0**, and
since p <= 128, symbol 0 is the less probable symbol.

**End-of-frame check.** This check is performed by the reference but not by
FFmpeg. After all NB x Nch symbols have been decoded (plus the DST_X_bit), the
frame is invalid if `ptr < fs - 7`. In other words, at most 7 code bits may
remain unread. The reference follows this with a loop that appears intended to
inspect the remaining bits, but it cannot change the result; that loop is a
no-op.

### 3.8 DST_X_bit

Immediately after initialisation, before bit 0 of any channel, the decoder
decodes one symbol with this probability:

    p_X = bitrev7(a0 mod 128) + 1

* `a0` is coefficient 0 of filter 0, taken as its 9-bit two's-complement
  pattern. `a0 mod 128` is its 7 least significant bits.
* `bitrev7` reverses the order of 7 bits. For example, 1 becomes 64, 2 becomes
  32, and 127 stays 127.
* p_X ranges from 1 to 128.

The decoded value is discarded. The reference does not use it, and nothing in
SACD defines it.

### 3.9 Output bit

The decoded symbol is `e`. The output bit is:

    y = s XOR e

where s is the sign indicator from 3.4. The bit y becomes `h_c[0]` (3.3), and
it is written to the output.

### 3.10 Output packing

The output frame has `FL x Nch` bytes. Bit n of channel c is stored in byte
`floor(n/8) x Nch + c`, at bit position `7 - (n mod 8)`, where 7 is the MSB.
Channels are therefore interleaved byte by byte, and within each byte the
earliest sample is the MSB.

The plain branch (DST_coded = 0) uses this same layout, so the raw payload is
copied unchanged.

### 3.11 Error handling

When any error occurs, the reference outputs the whole frame (all channels) as
the byte 0x55, which is DSD silence, and reports the error. Section 5 lists
the errors.

### 3.12 Comparison with FFmpeg / pysacd `dstdec.c`

These parts are identical, for a frame with one segment per channel:

* Bit-reader order.
* The plain branch, apart from zero-fill details.
* Mapping syntax: the `read_map` width is bitlen(T).
* `same_mapping` handling.
* Half-probability flags and the NH rule. FFmpeg uses the order of the
  channel's only filter.
* Filter-set syntax and reconstruction, including the rounding.
* Ptable reconstruction for lengths of 2 or more.
* The leading code bit `cb[0] = 0` check.
* Arithmetic decoder initialisation and the symbol rule. FFmpeg's `ac_get` is
  arithmetically identical, with the same q, w0, w1 and the same comparison.
* The DST_X_bit probability. FFmpeg uses an 8-bit reverse shifted right by 1,
  which equals bitrev7.
* The history initialisation 0xAA, the prediction, the 16-bit wrap, the
  ptable index, the output bit, and the packing.

For the differences, see section 7.

## 4. Constants and limits

| Item | Value |
|---|---|
| Channels Nch | 1 to 6 (external parameter) |
| Bits per channel per frame NB | 588 x fs44 (37632 at DSD64) |
| Bytes per channel per frame FL | 588 x fs44 / 8 (4704 at DSD64) |
| Max filters per frame NF | 2 x Nch |
| Max ptables per frame NP | 2 x Nch |
| Filter segments per channel | nominal max 4. The reference accepts up to 5 (shared) or 4 (per-channel), see 2.4. |
| Ptable segments per channel | nominal max 8. The reference accepts up to 9 (shared, overflowing its arrays) or 8 (per-channel). |
| Min filter segment | 1024 bits (128 bytes) |
| Min ptable segment | 32 bits (4 bytes) |
| Segment resolution R | 1 to FL - Lmin/8 bytes. At DSD64: 4576 for filters, 4700 for ptables. Field width 13 bits at DSD64. |
| Segment length unit | 8 x R bits |
| Prediction order N | 1 to 128 (7-bit field + 1) |
| Filter coefficient | 9-bit two's complement, -256 to 255 |
| Ptable length M | 1 to 64 (6-bit field + 1) |
| Ptable entry | 1 to 128 (7-bit field + 1) |
| Coefficient/entry coding methods | 0, 1, 2 (2-bit field; 3 invalid) |
| Rice parameter m | 3-bit field, 0 to 7. Nominal maxima 6 (filters) and 4 (ptables), not enforced. |
| Probability scale | 256. Half probability = 128. |
| Arithmetic registers | A and C are 12 bits. Renormalise while A < 2048. |
| Ptable index quantisation | floor(\|S\| / 8) |
| Prediction accumulator | wraps to 16-bit two's complement |
| Code bits allowed unread at end | at most 7 |
| Plain frame size | 1 + FL x Nch bytes |
| Frame duration | 1/75 s |

## 5. Error and validity rules (reference decoder)

Every rule below makes the frame invalid. The reference then outputs 0x55 for
the whole frame. The labels in brackets are this document's names for the
error classes.

1. **[data exhausted]** Reading any header field past the end of the frame is
   an error. Header fields are everything from `DST_coded` to the last ptable
   entry, except Rice codes, whose overrun the reference does not detect
   (2.10). Treat Rice-code overrun as this error too. The reference's message
   for this case is "negative number of bits allocated", which is misleading.
2. **[stuffing]** In a plain frame, the 6 bits after DST_X_bit_plain are not
   all zero.
3. **[resolution]** `segment_resolution` is 0 or greater than `FL - Lmin/8`.
4. **[segment length]** An explicit segment is shorter than Lmin bits
   (including L = 0), or leaves fewer than Lmin bits for the rest of the
   channel.
5. **[too many segments]** In shared segmentation, a further explicit segment
   is announced after Smax explicit lengths have already been read. In
   per-channel segmentation, Smax explicit lengths have been read for a
   channel and the decoder is about to read another `end_of_channel_segm`.
   (A third check, more than 8 ptable segments in a copied segmentation, is
   unreachable.)
6. **[table number]** A `table_index` is greater than the current count T.
7. **[too many tables]** The final table count for filters or ptables exceeds
   2 x Nch.
8. **[channel mapping]** `same_map_all_ch = 1`, but some channel has a
   different number of segments than channel 0 in that segmentation.
9. **[segment number mismatch]** `same_mapping = 1`, but for some channel the
   filter and ptable segmentations have different segment counts.
10. **[coefficient coding]** For a coded filter set, coding method k satisfies
    k + 1 >= N_f, or k = 3.
11. **[coefficient range]** A Rice-reconstructed filter coefficient is outside
    -256 to 255.
12. **[ptable coding]** For a coded ptable, k + 1 >= M_p, or k = 3.
13. **[ptable range]** A Rice-reconstructed ptable entry is outside 1 to 128.
14. **[arithmetic code]** fs > 0 and `cb[0] = 1`.
15. **[arithmetic decoder]** At the end of the frame, more than 7 code bits
    remain unread (`ptr < fs - 7`).

These are **not** checked by the reference:

* the value of either DST_X_bit;
* the range of the Rice parameter m;
* the values of code bits beyond the decoder's read position;
* the frame being longer than needed.

## 6. Encoder notes

This section states what an encoder must produce for a frame to be valid under
section 5. It also gives the arithmetic encoder that inverts 3.7 exactly.

### 6.1 Frame-level choices

* **Plain frame.** Write the byte 0x00: DST_coded = 0, DST_X_bit = 0, stuffing
  = 0. Follow it with the `FL x Nch` raw DSD bytes in the layout of 3.10. A
  real encoder falls back to this whenever the coded frame would be larger.
* **Simplest coded frame.** This is the form FFmpeg already accepts.
  * Write `same_segmentation = 1`, `same_segm_all_ch = 1`, and
    `end_of_channel_segm = 1`, so there is one segment per channel and no
    resolution field.
  * Then the mapping: `same_mapping`, `same_map_all_ch`, and per-channel
    indices as needed.

### 6.2 Segmentation

* Choose a resolution R, in bytes, with `1 <= R <= FL - Lmin/8`. When both
  segmentations are transmitted, one R is chosen for filters and one for
  ptables. Every explicit segment must be a multiple of `8 x R` bits.
* Send R exactly once per transmitted segmentation, immediately before the
  first explicit `segment_length` of that segmentation. In per-channel mode,
  that is in whichever channel first has an explicit segment.
  * If no channel has an explicit segment, do not send R.
  * With `same_segmentation = 1`, the ptables reuse the filter R.
* For each channel, the explicit lengths L_j must satisfy:
  * `8 x R x L_j >= Lmin`;
  * the remainder `NB - sum(8 x R x L_j)` must be at least Lmin;
  * the last segment is implicit and is not sent.
* Write each `segment_length` in exactly `bitlen(floor(remBytes / R))` bits,
  where remBytes is `FL - Lmin/8` minus the bytes of the channel's earlier
  explicit segments. Valid lengths always fit in that width.
* Keep the total per channel at **Smax segments or fewer** (at most Smax - 1
  explicit lengths): 4 for filters, 8 for ptables. This is valid in both modes
  of the reference. Note that per-channel mode rejects an encoding with Smax
  explicit lengths.
* Shared mode (`same_segm_all_ch = 1`) is a single list. Write it as
  `end_of_channel_segm` = 0, then [R], then L, then `end_of_channel_segm` = 0,
  then L, and so on, ending with `end_of_channel_segm` = 1.
* Per-channel mode is written for each channel as zero or more groups of
  (`end_of_channel_segm` = 0, [R], L), followed by `end_of_channel_segm` = 1.

### 6.3 Mapping

* Number the tables in order of first use, scanning (channel 0 segments 0 ..
  n-1), then channel 1, and so on. Segment 0 of channel 0 is always table 0.
* Write each index in `bitlen(T)` bits, where T is the number of tables
  introduced so far.
* At most `2 x Nch` filters and `2 x Nch` ptables are allowed.
* `same_map_all_ch = 1` requires an equal segment count in every channel for
  that segmentation.
* `same_mapping = 1` requires the filter and ptable segment counts to be equal
  in each channel, though the lengths may differ. The ptable of segment j is
  then the filter number of segment j, and NP = NF.
* Every table number from 0 to T-1 must have a coefficient set or ptable in
  the frame, even if it is used only once.

### 6.4 Filters and probability tables

* Filters are written in table-number order: the 7-bit `N-1`, then the
  `coef_coded` bit.
  * Verbatim: write N 9-bit two's-complement values.
  * Coded: requires `k + 1 < N`. Write k, then the first k+1 coefficients
    verbatim, then m, then for i = k+1 .. N-1 the residual
    `r_i = a_i + floor((x_i + 4) / 8)`, with x_i as in 2.7, computed from the
    true coefficients. Rice-code each residual (6.5).
* Ptables follow the same pattern: write `M-1` in 6 bits.
  * If M = 1, write nothing further. The decoder then uses entry 128, so a
    one-entry table always has probability 128.
  * If M > 1, write `ptable_coded`.
  * Verbatim: write each entry minus 1 in 7 bits.
  * Coded: requires `k + 1 < M`. Write k, then the first k+1 entries minus 1
    in 7 bits, then m, then the residuals `r_i = P[i] + floor((x_i + 4) / 8)`,
    with x_i from 2.8.
  * All entries must lie in 1 to 128.
* Keep m <= 6 for filters and m <= 4 for ptables. These are the nominal limits;
  the reference does not enforce them.
* Choose ptable entries so that `p / 256` approximates the probability that the
  coded symbol is 0, that is, that the output bit equals the sign indicator s.
  Any value in 1 to 128 is decodable; the choice affects only compression.

### 6.5 Rice encoding of a residual r with parameter m

1. Let `u = |r|`.
2. Write `floor(u / 2^m)` zero bits, then a 1.
3. Write `u mod 2^m` in m bits, MSB first.
4. If r != 0, write the sign bit: 1 if r < 0, 0 if r > 0.

### 6.6 Symbols to encode

* Run the same history (3.3), prediction (3.4) and probability selection
  (3.5, 3.6) as the decoder, in the order given in 3.2.
* For the DST_X_bit, encode the symbol 0 with `p_X` from 3.8. Any value
  decodes; 0 is recommended.
* For each actual bit y, encode `e = y XOR s`.

### 6.7 Arithmetic encoder (exact inverse of 3.7)

**State.**

* `Lo`, a non-negative integer of unbounded precision; in practice a bit
  buffer.
* `A`, 12 bits.
* `K`, the number of renormalisation shifts so far.

**Start.** Set Lo = 0, A = 4095, K = 0.

**Encoding symbol e with probability p.**

1. Compute q, w0 and w1 exactly as in 3.7 from the current A.
2. If e = 1: `A = w1`, and Lo is unchanged.
   If e = 0: `Lo = Lo + w1` and `A = w0`.
3. While A < 2048: `A = 2A`, `Lo = 2Lo`, `K = K + 1`.

**Invariant.** The decoder's register satisfies

    C = floor(V x 2^K) - Lo,    where V = sum over i >= 1 of cb[i] x 2^(12 - i)

The value V is the code read as a 12.x fixed-point number. The decoder chooses
e = 0 exactly when `floor(V x 2^K) >= Lo + w1`. The intervals
`[Lo, Lo + A) / 2^K` are nested, starting from `[0, 4095)`, so a final V that
lies in the last interval decodes every symbol correctly.

**Carry.** Adding w1 to Lo can carry into bits that a streaming encoder has
already produced. Either:

* keep the code as a bit array and propagate each carry towards cb[1], or
* use the usual pending-ones counting technique.

A carry can never reach cb[0]. Every interval lies inside `[0, 4095 x 2^K)`,
so `Lo < 2^(12+K)`, and that is why `cb[0] = 0` always holds.

**Termination.**

1. After the last symbol (and its renormalisation) set K_f = K, with interval
   `[Lo, Lo + A)` and `A >= 2048`. Choose

       Mv = 2^11 x ceil(Lo / 2^11)

   Any run of 2048 consecutive integers contains a multiple of 2^11, so Mv
   lies in `[Lo, Lo + A)`.
2. The codeword is `cb[0] = 0`, followed by the `K_f + 1` bits of
   `floor(Mv / 2^11)`, MSB first, as cb[1] .. cb[K_f + 1]. Every later bit of
   Mv is 0, so it does not need to be sent: the decoder reads past-the-end
   bits as 0 and so reconstructs V exactly.
   * Any Mv in the interval works. A multiple of a larger power of two gives a
     shorter code if one exists in the interval.
3. Code length: `n = K_f + 2` bits, including cb[0].

**Padding and frame length.**

1. Let H be the number of header bits, from `DST_coded` to the last ptable
   field.
2. Choose the frame length `FB = ceil((H + n) / 8)` bytes, so that
   `fs = 8 x FB - H` lies between n and n + 7.
3. Fill the `fs - n` padding bits with 0.

**End-of-frame check.** The decoder finishes with `ptr = 13 + K_f`, and the
check requires `fs <= K_f + 20`. With this termination,
`fs <= K_f + 9 <= K_f + 20`, so the check always passes.

* Do not append extra bytes. The check allows at most 7 unread code bits.
  A frame padded by many more bytes than needed is rejected by the reference,
  though not by FFmpeg.
* Truncation is allowed: dropping trailing zero bits of the codeword (so that
  fs < n) is valid because missing bits read as 0. The end-of-frame check sets
  only an upper bound on fs, so it still passes.

**Edge case: no code bits.** If H already fills the frame exactly, fs = 0.
Never produce this; always emit at least cb[0].

## 7. Differences from FFmpeg's decoder (what `csrc/dstdec.c` must change)

Each item describes behaviour.

1. **Segmentation (new).**
   * Parse `same_segmentation`, `same_segm_all_ch` and `end_of_channel_segm`
     fully, as in 2.3 and 2.4, instead of rejecting a frame whenever any of
     the three bits is 0.
   * Support filter and ptable segmentations, shared and per-channel, with the
     resolution field, the length fields with running widths, the implicit
     last segment, and the validity rules.
   * Copy the filter segmentation into the ptable segmentation when
     `same_segmentation = 1`.
   * The widths depend on FL, so on fs44. pysacd fixes fs44 = 64.
2. **Mapping per segment (changed).**
   * Map table numbers per (channel, segment), not per channel.
   * With `same_map_all_ch = 1`, read indices for segments 1 .. nseg[0]-1 of
     channel 0 and copy them to every channel. The segment counts must be
     equal.
   * With `same_map_all_ch = 0`, read an index for every (channel, segment)
     except (0, 0).
   * With `same_mapping = 1`, require equal per-channel segment counts between
     the filter and ptable segmentations.
3. **Table-count limit (changed).**
   * FFmpeg fails as soon as the running count reaches 12, a constant. The
     reference fails only if the final count exceeds `2 x Nch`, and checks
     once, after the whole mapping.
   * This differs in both directions. In stereo, the reference rejects 5 or
     more filters. FFmpeg would reject a valid 6-channel frame with 12 tables.
4. **Per-bit table selection (new).**
   * For each bit n, select the filter F(c, n) and the ptable P(c, n) from the
     segment that contains n (3.1). The ptable length used for the index clamp
     is that of P(c, n).
   * The prediction history is per channel and is not reset at segment
     boundaries.
5. **Half-probability length (clarified).** NH[c] is the order of the filter
   of channel c's first filter segment. It is not the order of the filter in
   force at bit n.
6. **One-entry probability tables (changed; affects parsing even without
   segmentation).**
   * When the coded ptable length is 1, the reference reads **no**
     `ptable_coded` bit and **no** entry, and sets the entry to 128.
   * FFmpeg always reads the coded bit and then at least one 7-bit entry, so
     it desynchronises from the reference on such tables.
   * Filters are unaffected: their coded bit is always present.
7. **Coding-method order check (new).** Reject a coded filter set with
   `k + 1 >= N_f`, and a coded ptable with `k + 1 >= M_p`. FFmpeg accepts
   these and reads k+1 verbatim values, which can be more than the table
   length. Keep rejecting k = 3.
8. **Filter-coefficient range (new).**
   * Reject Rice-reconstructed filter coefficients outside -256 to 255.
   * FFmpeg does not check this. Its only check is that each 8-tap lookup
     entry fits in 16 bits, and that check cannot fail for in-range
     coefficients.
   * Ptable range checking (1 to 128) already matches.
9. **End-of-frame check (new, optional but reference-conformant).**
   * Track how many code bits the arithmetic decoder has consumed, counting
     the 12 initial bits plus cb[0] as 13, plus 1 for every renormalisation
     shift.
   * After the last symbol, report an error if that count is less than
     `fs - 7`.
   * FFmpeg performs no such check.
10. **Rice-code overrun.**
    * FFmpeg's Golomb reader limits the unary run to the bits left in the
      frame. At exactly `limit - 1` zeros, it returns the escape value 1.
    * The reference has no limit.
    * Neither is specified behaviour. Treat reaching the end of the frame
      inside a Rice code as an error rather than returning 1.
11. **Errors.**
    * The reference turns every error into a frame of 0x55 bytes, which is
      DSD silence.
    * pysacd returns an error code instead. Either behaviour is acceptable,
      but the caller should substitute silence (0x55, not 0x00) if it wants
      the reference's output.
    * FFmpeg's `size <= 1` rejection has no counterpart in the reference.
      Keep it; it is harmless.
12. **Unchanged.** These need no change:
    * the plain-frame branch;
    * the leading code bit check;
    * the arithmetic decoder arithmetic;
    * the DST_X_bit probability;
    * the history initialisation;
    * the prediction and its 16-bit wrap;
    * the ptable index rule;
    * the output-bit rule;
    * the output packing;
    * the `same_mapping` and `same_map_all_ch` handling in the single-segment
      case.

    Note: the history update through 64-bit `memcpy` assumes a little-endian
    host. That is not a format difference, but a portable rewrite should use a
    logical shift register.

## 8. Open questions and uncertainties

* **Segment-count limit.** The reference allows Smax + 1 segments when
  segmentation is shared across channels, and Smax when it is per-channel
  (2.4). For ptables in shared mode, Smax + 1 = 9 overflows the reference's own
  storage. The standard's text was not available. The constant names suggest
  the intended limit is Smax segments per channel.
  * Recommended decoder policy: mirror the reference's acceptance, with arrays
    for 9 segments.
  * Recommended encoder policy: never exceed Smax.
* **Coding method 3.** The reference has no explicit check and reads past its
  method tables. Treating it as invalid is the only sane choice.
* **One-entry ptables.** It is unknown whether real SACD streams ever contain
  ptables of length 1. If they do, FFmpeg's parser would desynchronise on them,
  so their absence from commercial discs is plausible but unverified.
* **DST_X_bit semantics.** Both DST_X_bits, the raw bit in plain frames and
  the arithmetic-coded one in coded frames, have no defined use for SACD. The
  reference ignores both.
* **Rice parameter maxima** (6 for filters, 4 for ptables) appear only as
  constants in the reference and are not enforced.
* **fs44 above 64.** The format scales with fs44 through FL and NB as in
  section 1, but the reference decoder only works at fs44 = 64, so the
  behaviour at higher rates was not observed.

### Errata from tests against the reference decoder

These were found by encoding frames from section 6 with a test encoder and
decoding them both with pysacd and with the reference decoder (inside
`sacd_extract`, used as a black box) from synthetic SACD images, at 2, 5 and
6 channels. Where the reference disagreed with the text above, the
reference wins.

* **Minimum segment length (2.4, rule 5.4).** The sentence "This is the same
  as saying `L >= 1` and `R x L <= remBytes`" is wrong whenever
  `R < Lmin/8`. The reference rejects every explicit segment with
  `8 x R x L < Lmin`, even when `L >= 1`. Observed: filter segments of 127
  bytes (R = 1, L = 127) and 120 bytes (R = 8, L = 15), and ptable segments
  of 3 bytes (R = 1, L = 3) and 2 bytes (R = 2, L = 1), are all rejected. The
  check is therefore `Lmin <= 8 x R x L` **and** `R x L <= remBytes`.
* **Nine ptable segments (2.4, section 8 "Segment-count limit").** Shared
  ptable segmentation with Smax = 8 explicit lengths (9 segments) is not
  decodable by the reference. The ninth segment is decoded with the wrong
  table (the result is correct only when its table number equals the table
  of segment 0, consistent with the write spilling into the next channel's
  first entry), and at 6 channels every such frame is rejected. Decoders
  should treat a ninth ptable segment as invalid (pysacd does), and the
  recommendation to accept it is withdrawn. The filter case is sound: shared
  filter segmentation with 4 explicit lengths (5 segments) decodes
  correctly with any table assignment at 2, 5 and 6 channels, so 5 filter
  segments remain valid in shared mode. Per-channel limits behave as stated
  (3 filter lengths and 7 ptable lengths accepted, one more rejected).
* **Arithmetic code longer than NB x Nch bits (2.9, 6.7).** The reference
  keeps the code bits in a fixed array of NB x Nch entries. A frame with
  `fs > NB x Nch` overflows it: at 2 channels, fs = 75273 and 75274 bits
  crashed `sacd_extract` with heap corruption, while fs = 75271 decoded
  correctly. The crash is not about the frame length: frames up to 89 bytes
  longer than a plain frame decode correctly when fs <= NB x Nch.
  Consequences:
  * An encoder must keep `fs <= NB x Nch`. Falling back to a plain frame
    when the code would be longer (6.1) achieves this.
  * A frame that codes every bit with probability 1/2 (for example, every
    channel using one-entry ptables for the whole frame) always needs a few
    bits more than NB x Nch, so the reference can never decode it. One-entry
    ptables themselves are fine, and are parsed as 2.8 says: frames where
    they cover part of the frame or some channels decode identically.
  * pysacd decodes such frames correctly; this is not an error condition
    of the format, but no conforming stream contains them.
* **End-of-frame check (3.7, 6.7), clarified.** The reference behaves as
  stated. Because the decoder's 12-bit window ends 11 bits past the last
  codeword bit, one appended zero byte is always accepted, two are accepted
  only when the original padding was at most 2 bits, and three are always
  rejected. Dropping trailing zero bytes is accepted.
* **Error output (3.11), confirmed.** Every rejected frame came out of the
  reference as 0x55 bytes, with the neighbouring frames unaffected. A
  plain-frame header with DST_X_bit_plain = 1 is accepted; non-zero stuffing
  is rejected.
* **Short plain frames (2.2).** A plain frame shorter than `1 + FL x Nch`
  bytes crashed `sacd_extract` (segmentation fault) at 2 and 6 channels, so
  the zero-fill behaviour could not be observed. pysacd zero-fills as
  recommended. Longer plain frames are accepted and the extra bytes are
  ignored, as stated.
