# Builds the native helper src/pysacd/_sacdconv.so (DSF/DSDIFF/WAV always, FLAC if libFLAC is found),
# with the scarletbook-rs C library linked in statically (#85).
#   make                 build, tuned for this CPU (-march=native)
#   make PORTABLE=1      build for generic x86-64
#   make HARDEN=0        without the hardening flags (sanitizer builds)
#   make clean
#   make test-tools      also build the test-only DST encoder (build/dstenc.so)
#   make SCARLETBOOK_DIR=DIR   use the scarletbook C library unpacked in DIR (include/, lib/,
#                        native-static-libs.txt) instead of downloading the pinned release

.DEFAULT_GOAL := all

UNAME_S := $(shell uname -s)
UNAME_M := $(shell uname -m)
ifeq ($(OS),Windows_NT)
# Windows: MinGW-w64 gcc (MSYS2 UCRT64). Everything is linked statically
# (winpthreads, libgcc, libFLAC), so the DLL needs nothing beside it (#8).
CC      ?= gcc
LIBEXT  := dll
SHARED  := -shared -static -static-libgcc
NATIVE  := -march=native
PIC     :=
PKGFLAGS := --static
FLAC_DEFS := -DFLAC__NO_DLL
# C99 printf (%zu) instead of msvcrt's
OS_DEFS  := -D__USE_MINGW_ANSI_STDIO=1
# scarletbook (#85): every member of the archive, so that all of its sb_* functions are in the
# DLL for ctypes. A .def file lists what the DLL exports, the engine's functions and scarletbook's
# C ABI, generated from their sources; without one, MinGW's ld would export Rust's internals too.
SB_WHOLE = -Wl,--whole-archive $(1) -Wl,--no-whole-archive
SB_EXPORTS = $(BUILD)/exports.def -Wl,--gc-sections
SB_EXPORTS_DEP = $(BUILD)/exports.def
else ifeq ($(UNAME_S),Darwin)
# macOS: clang, a .dylib, and -mcpu=native on Apple silicon (#8)
CC      ?= cc
LIBEXT  := dylib
SHARED  := -dynamiclib
NATIVE  := $(if $(filter arm64,$(UNAME_M)),-mcpu=native,-march=native)
SB_WHOLE = -Wl,-force_load,$(1)
# export the engine's functions and scarletbook's C ABI only, not Rust's internals; drop the rest
SB_EXPORTS := -Wl,-exported_symbols_list,csrc/exports.darwin -Wl,-dead_strip
SB_EXPORTS_DEP := csrc/exports.darwin
else
CC      ?= gcc
LIBEXT  := so
SHARED  := -shared
NATIVE  := -march=native
SB_WHOLE = -Wl,--whole-archive $(1) -Wl,--no-whole-archive
# export the engine's functions and scarletbook's C ABI only, not Rust's internals; drop the rest
SB_EXPORTS := -Wl,--version-script=csrc/exports.map -Wl,--gc-sections
SB_EXPORTS_DEP := csrc/exports.map
endif
ifdef PORTABLE
# generic x86-64, with the PCM filters picked at load time for the CPU (see csrc/dsp.c)
ARCH    := -DSC_DISPATCH
else
ARCH    := $(NATIVE)
endif
CFLAGS  ?= -O3 -ffast-math -fno-finite-math-only $(ARCH)
# override: required flags survive `make CFLAGS=...` on the command line
PIC     ?= -fPIC
override CFLAGS += $(PIC) $(OS_DEFS) -std=gnu17 -Wall -pthread
LDLIBS  := -lm -pthread

# Hardening (#14): fortified libc calls, stack protector and stack-clash protection, CET on
# x86, full RELRO and a non-executable stack. Linux only: macOS's ld and MinGW reject the -z
# flags. Sanitizer builds turn it off with HARDEN=0 (_FORTIFY_SOURCE gets in ASan's way and
# needs optimisation). Cost: docs/performance.md#hardening.
HARDEN ?= 1
ifeq ($(HARDEN)$(UNAME_S),1Linux)
ifneq ($(OS),Windows_NT)
HARDEN_CFLAGS  := -U_FORTIFY_SOURCE -D_FORTIFY_SOURCE=3 -fstack-protector-strong -fstack-clash-protection
HARDEN_LDFLAGS := -Wl,-z,relro,-z,now,-z,noexecstack
ifneq ($(filter x86_64 i686,$(UNAME_M)),)
HARDEN_CFLAGS  += -fcf-protection
endif
endif
endif
override CFLAGS  += $(HARDEN_CFLAGS)
override LDFLAGS += $(HARDEN_LDFLAGS)

PKG_CONFIG  ?= pkg-config
FLAC_CFLAGS := $(shell $(PKG_CONFIG) $(PKGFLAGS) --cflags flac 2>/dev/null)
FLAC_LIBS   := $(shell $(PKG_CONFIG) $(PKGFLAGS) --libs flac 2>/dev/null)
ifneq ($(FLAC_LIBS),)
override CFLAGS += -DHAVE_FLAC $(FLAC_DEFS) $(FLAC_CFLAGS)
LDLIBS  += $(FLAC_LIBS)
ifeq ($(shell $(PKG_CONFIG) --atleast-version=1.5.0 flac && echo y),y)
override CFLAGS += -DHAVE_FLAC_MT
endif
endif

# The scarletbook-rs C library (#85), linked statically: the release pinned in scarletbook.lock,
# downloaded and checked against its SHA-256 by tools/scarletbook.py into build/scarletbook, for
# the target $(CC) builds for. SCARLETBOOK_DIR=DIR (make's command line or the environment) uses
# a library unpacked there instead, with the release archive's layout; nothing is downloaded.
PYTHON ?= python3
ifeq ($(origin SCARLETBOOK_DIR),undefined)
SCARLETBOOK_DIR := build/scarletbook
SB_READY := $(SCARLETBOOK_DIR)/.pysacd-scarletbook
$(SB_READY): scarletbook.lock tools/scarletbook.py
	$(PYTHON) tools/scarletbook.py fetch --cc "$(CC)" --dest $(SCARLETBOOK_DIR)
else
SB_READY := $(SCARLETBOOK_DIR)/include/scarletbook.h
endif
SB_LIB  := $(SCARLETBOOK_DIR)/lib/libscarletbook_ffi.a
# read when linking, after the library has been fetched
SB_LIBS = $(shell cat $(SCARLETBOOK_DIR)/native-static-libs.txt)
# Rust's debug information makes the library ten times larger: leave it out unless CFLAGS asks for -g
SB_STRIP = $(if $(filter -g -g%,$(CFLAGS)),,-Wl,-S)
override CFLAGS += -I$(SCARLETBOOK_DIR)/include

SRC := $(addprefix csrc/,sacdconv.c dsp.c)
# BUILD and LIB can be overridden: the wheel build (setup.py) uses a private directory.
BUILD ?= build
OBJ := $(patsubst csrc/%.c,$(BUILD)/%.o,$(SRC))
LIB ?= src/pysacd/_sacdconv.$(LIBEXT)

all: $(LIB)
ifeq ($(FLAC_LIBS),)
	@echo "note: libFLAC not found; built without FLAC output (install flac-devel and 'make clean all')"
endif

$(LIB): $(OBJ) $(SB_READY) $(SB_EXPORTS_DEP)
	$(CC) $(SHARED) $(CFLAGS) $(LDFLAGS) $(SB_EXPORTS) $(SB_STRIP) -o $@ $(OBJ) \
	    $(call SB_WHOLE,$(SB_LIB)) $(SB_LIBS) $(LDLIBS)

# Windows: every function declared in scarletbook.h and every sacdconv_* function defined in
# csrc/sacdconv.c (each declaration starts its line: `type name(` or `type *name(`)
$(BUILD)/exports.def: $(SB_READY) csrc/sacdconv.c
	@mkdir -p $(dir $@)
	{ echo EXPORTS; \
	  sed -n 's/^[A-Za-z][A-Za-z0-9_ ]*[ *]\(sb_[a-z0-9_]*\)(.*/    \1/p' $(SCARLETBOOK_DIR)/include/scarletbook.h; \
	  sed -n 's/^[A-Za-z][A-Za-z0-9_ ]*[ *]\(sacdconv_[a-z0-9_]*\)(.*/    \1/p' csrc/sacdconv.c; } > $@

$(BUILD)/%.o: csrc/%.c $(wildcard csrc/*.h) $(SB_READY)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c -o $@ $<

# Test-only DST encoder core used by tests/dstenc.py (not part of the package).
# No -ffast-math: the synthetic test signal should not depend on it.
TEST_TOOLS := build/dstenc.so

test-tools: $(TEST_TOOLS)

build/dstenc.so: tests/native/dstenc.c
	@mkdir -p $(dir $@)
	$(CC) -O2 $(ARCH) $(PIC) $(SHARED) -std=gnu17 -Wall -o $@ $< -lm

# libFuzzer harness for pysacd's engine on hostile disc images (#36, #85): clang only.
#   make fuzz && build/fuzz_engine -max_total_time=300 build/fuzz-corpus/engine build/fuzz-seeds/engine
# scarletbook-rs, which parses the images, is linked as released (it is fuzzed in its own
# repository); the engine's own code is instrumented. No libFLAC: FLAC output isn't fuzzed.
FUZZ_CC    ?= clang
FUZZ_FLAGS := -O1 -g -fno-omit-frame-pointer -fsanitize=fuzzer,address,undefined \
              -fno-sanitize-recover=undefined -std=gnu17 -pthread -DSC_DISPATCH
FUZZ_SRC   := csrc/sacdconv.c csrc/dsp.c
# The same harness without libFuzzer, for any compiler: tests/test_fuzz_engine.py replays the
# regression inputs and seeds with it (REPLAY_FLAGS: the sanitizer CI job's flags)
REPLAY_FLAGS ?= -O1 -g

fuzz: build/fuzz_engine

build/fuzz_engine: fuzz/fuzz_engine.c $(FUZZ_SRC) $(wildcard csrc/*.h) $(SB_READY)
	@mkdir -p $(dir $@)
	$(FUZZ_CC) $(FUZZ_FLAGS) -I$(SCARLETBOOK_DIR)/include -o $@ $< $(FUZZ_SRC) $(SB_LIB) $(SB_LIBS) -lm

fuzz-replay: build/replay_engine

build/replay_engine: fuzz/fuzz_engine.c fuzz/replay.c $(FUZZ_SRC) $(wildcard csrc/*.h) $(SB_READY)
	@mkdir -p $(dir $@)
	$(CC) $(REPLAY_FLAGS) -std=gnu17 -pthread -DSC_DISPATCH -I$(SCARLETBOOK_DIR)/include -o $@ \
	    fuzz/fuzz_engine.c fuzz/replay.c $(FUZZ_SRC) $(SB_LIB) $(SB_LIBS) -lm

# `make clean` keeps the downloaded scarletbook library (build/scarletbook); distclean removes it too.
clean:
	find build -mindepth 1 -maxdepth 1 ! -name scarletbook -exec rm -rf {} + 2>/dev/null || true
	rm -rf src/pysacd/_sacdconv.so src/pysacd/_sacdconv.dylib src/pysacd/_sacdconv.dll

distclean: clean
	rm -rf build

.PHONY: all clean distclean test-tools fuzz fuzz-replay
