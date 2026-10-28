# Builds the native helper src/pysacd/_sacdconv.so (DSF/DSDIFF/WAV always, FLAC if libFLAC is found).
#   make                 build, tuned for this CPU (-march=native)
#   make PORTABLE=1      build for generic x86-64
#   make HARDEN=0        without the hardening flags (sanitizer builds)
#   make clean
#   make test-tools      also build the test-only DST encoder (build/dstenc.so)

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
else ifeq ($(UNAME_S),Darwin)
# macOS: clang, a .dylib, and -mcpu=native on Apple silicon (#8)
CC      ?= cc
LIBEXT  := dylib
SHARED  := -dynamiclib
NATIVE  := $(if $(filter arm64,$(UNAME_M)),-mcpu=native,-march=native)
else
CC      ?= gcc
LIBEXT  := so
SHARED  := -shared
NATIVE  := -march=native
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

SRC := $(addprefix csrc/,sacdconv.c demux.c dsp.c dstdec.c)
# BUILD and LIB can be overridden: the wheel build (setup.py) uses a private directory.
BUILD ?= build
OBJ := $(patsubst csrc/%.c,$(BUILD)/%.o,$(SRC))
LIB ?= src/pysacd/_sacdconv.$(LIBEXT)

all: $(LIB)
ifeq ($(FLAC_LIBS),)
	@echo "note: libFLAC not found; built without FLAC output (install flac-devel and 'make clean all')"
endif

$(LIB): $(OBJ)
	$(CC) $(SHARED) $(CFLAGS) $(LDFLAGS) -o $@ $^ $(LDLIBS)

$(BUILD)/%.o: csrc/%.c $(wildcard csrc/*.h)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c -o $@ $<

# Test-only DST encoder core used by tests/dstenc.py (not part of the package).
# No -ffast-math: the synthetic test signal should not depend on it.
TEST_TOOLS := build/dstenc.so

test-tools: $(TEST_TOOLS)

build/dstenc.so: tests/native/dstenc.c
	@mkdir -p $(dir $@)
	$(CC) -O2 $(ARCH) $(PIC) $(SHARED) -std=gnu17 -Wall -o $@ $< -lm

# libFuzzer harnesses for the code that parses untrusted disc data (#36): clang only.
#   make fuzz && build/fuzz_dst_frame -max_total_time=300 build/fuzz-corpus/dst_frame build/fuzz-seeds/dst_frame
FUZZ_CC    ?= clang
FUZZ_FLAGS := -O1 -g -fno-omit-frame-pointer -fsanitize=fuzzer,address,undefined \
              -fno-sanitize-recover=undefined -std=gnu17
FUZZ_SRC   := csrc/demux.c csrc/dstdec.c

fuzz: build/fuzz_dst_frame build/fuzz_demux

build/fuzz_%: fuzz/fuzz_%.c $(FUZZ_SRC) $(wildcard csrc/*.h)
	@mkdir -p $(dir $@)
	$(FUZZ_CC) $(FUZZ_FLAGS) -o $@ $< $(FUZZ_SRC) -lm

clean:
	rm -rf build src/pysacd/_sacdconv.so src/pysacd/_sacdconv.dylib src/pysacd/_sacdconv.dll

.PHONY: all clean test-tools fuzz
