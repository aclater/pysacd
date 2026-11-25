# Builds the native helper src/pysacd/_sacdconv.so (DSF/DSDIFF/WAV always, FLAC if libFLAC is found),
# with the scarletbook-rs C library (#85) and the dsd2pcm C library linked in statically.
#   make                 build, tuned for this CPU (-march=native)
#   make PORTABLE=1      build for generic x86-64
#   make HARDEN=0        without the hardening flags (sanitizer builds)
#   make clean
#   make test-tools      also build the test-only DST encoder (build/dstenc.so)
#   make SCARLETBOOK_DIR=DIR   use the scarletbook C library unpacked in DIR (include/, lib/,
#                        native-static-libs.txt) instead of downloading the pinned release
#   make DSD2PCM_DIR=DIR use the dsd2pcm C library in DIR (include/dsd2pcm.h, lib/libdsd2pcm.a)
#                        instead of tools/dsd2pcm.py's (the pinned release, or built from its source)

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
# dsd2pcm's archive also holds import-library members (sections .idata$*) for the DLLs Rust's
# std links by raw-dylib. ld -r would merge them, which breaks the DLL's import table, and would
# merge the LLVM bitcode sections (.llvmbc) into one that ld then places outside the image:
# Windows refuses such a DLL ("not a valid Win32 application"). So the import members go into an
# archive of their own for the final link (D2P_IMPORTS), and the bitcode is dropped.
D2P_IMPORTS = $(BUILD)/dsd2pcm-imports.a
D2P_PRELINK = imps=$$($(OBJDUMP) -h $(2) | awk '/file format/ {m=$$1; sub(/:$$/,"",m)} /\.idata\$$/ {print m}' | sort -u) && \
    cp $(2) $(1).code.a && $(AR) d $(1).code.a $$imps && \
    cp $(2) $(D2P_IMPORTS) && $(AR) d $(D2P_IMPORTS) $$($(AR) t $(1).code.a) && \
    $(CC) -r -nostdlib -o $(1).tmp -Wl,--whole-archive $(1).code.a -Wl,--no-whole-archive && \
    $(OBJCOPY) --remove-section=.llvmbc --remove-section=.llvmcmd --wildcard --keep-global-symbol='dsd2pcm_*' \
        $(1).tmp $(1) && rm -f $(1).tmp $(1).code.a
SB_SYMTOOL := objdump
SB_SYMS = -p $(1)
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
# ld -r turns the symbols it doesn't export into local ones (unless -keep_private_externs)
D2P_PRELINK = $(CC) -r -nostdlib -o $(1) -Wl,-exported_symbol,'_dsd2pcm_*' -Wl,-force_load,$(2)
SB_EXPORTS_HAS := ^_sb_\*$$
SB_SYMTOOL := nm
SB_SYMS = -gU $(1)
else
CC      ?= gcc
LIBEXT  := so
SHARED  := -shared
NATIVE  := -march=native
SB_WHOLE = -Wl,--whole-archive $(1) -Wl,--no-whole-archive
# export the engine's functions and scarletbook's C ABI only, not Rust's internals; drop the rest
SB_EXPORTS := -Wl,--version-script=csrc/exports.map -Wl,--gc-sections
SB_EXPORTS_DEP := csrc/exports.map
# x86-64 glibc: the object is marked for CET shadow stacks, which Rust's toolchains don't do, so
# that the engine keeps its mark (#94; tools/mark_shstk.py says why that is sound). Not on musl,
# which never turns shadow stacks on, and whose startup objects carry no mark anyway.
D2P_MARK := $(and $(filter x86_64,$(UNAME_M)),$(if $(findstring musl,$(shell $(CC) -dumpmachine 2>/dev/null)),,y))
D2P_PRELINK = $(CC) -r -nostdlib -o $(1).tmp -Wl,--whole-archive $(2) -Wl,--no-whole-archive && \
    $(OBJCOPY) --remove-section=.group --wildcard --keep-global-symbol='dsd2pcm_*' $(1).tmp $(1) && rm -f $(1).tmp \
    $(if $(D2P_MARK),&& $(PYTHON) tools/mark_shstk.py --objcopy $(OBJCOPY) $(1))
SB_EXPORTS_HAS := ^ *sb_\*;$$
SB_SYMTOOL := nm
SB_SYMS = -D --defined-only $(1)
endif
OBJCOPY ?= objcopy
OBJDUMP ?= objdump
ifdef PORTABLE
# generic x86-64 (the PCM conversion, dsd2pcm's, picks its SIMD level at run time)
ARCH    := -DPYSACD_PORTABLE
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
# BUILD and LIB can be overridden: the wheel build (setup.py) uses a private directory.
BUILD ?= build
ifeq ($(origin SCARLETBOOK_DIR),undefined)
SCARLETBOOK_DIR := build/scarletbook
SB_READY := $(SCARLETBOOK_DIR)/.pysacd-scarletbook
$(SB_READY): scarletbook.lock tools/scarletbook.py
	$(PYTHON) tools/scarletbook.py fetch --cc "$(CC)" --dest $(SCARLETBOOK_DIR)
else
ifeq ($(OS),Windows_NT)
# forward slashes: the recipes run under MSYS's sh, which takes a backslash as an escape (#225)
override SCARLETBOOK_DIR := $(subst \,/,$(SCARLETBOOK_DIR))
endif
SB_READY := $(SCARLETBOOK_DIR)/include/scarletbook.h
endif
SB_LIB  := $(SCARLETBOOK_DIR)/lib/libscarletbook_ffi.a
# read when linking, after the library has been fetched
SB_LIBS = $(shell cat $(SCARLETBOOK_DIR)/native-static-libs.txt)
# Rust's debug information makes the library ten times larger: leave it out unless CFLAGS asks for -g
SB_STRIP = $(if $(filter -g -g%,$(CFLAGS)),,-Wl,-S)
override CFLAGS += -I$(SCARLETBOOK_DIR)/include

# The dsd2pcm C library (dsd2pcm.lock): its precise and pysacd filters convert DSD to PCM. tools/dsd2pcm.py
# downloads the pinned release's archive for the target $(CC) builds for, checked against its
# SHA-256, or builds it from the pinned source where the release has none (that needs cargo).
# DSD2PCM_DIR=DIR uses a library there instead. Two Rust static libraries can't be linked side by
# side (each carries Rust's standard library), so dsd2pcm's is first linked into one object whose
# only global symbols are dsd2pcm_* (D2P_PRELINK); scarletbook's runtime and dsd2pcm's then
# never meet, and nothing of dsd2pcm is exported.
ifeq ($(origin DSD2PCM_DIR),undefined)
DSD2PCM_DIR := build/dsd2pcm
D2P_READY := $(DSD2PCM_DIR)/.pysacd-dsd2pcm
$(D2P_READY): dsd2pcm.lock tools/dsd2pcm.py tools/scarletbook.py
	$(PYTHON) tools/dsd2pcm.py fetch --cc "$(CC)" --dest $(DSD2PCM_DIR)
else
D2P_READY := $(DSD2PCM_DIR)/include/dsd2pcm.h
endif
D2P_OBJ := $(BUILD)/dsd2pcm-prelinked.o
override CFLAGS += -I$(DSD2PCM_DIR)/include

$(D2P_OBJ): $(D2P_READY) tools/mark_shstk.py
	@mkdir -p $(dir $@)
	$(call D2P_PRELINK,$@,$(DSD2PCM_DIR)/lib/libdsd2pcm.a)

SRC := $(addprefix csrc/,sacdconv.c)
# LIB can be overridden: the wheel build (setup.py) uses a private directory (BUILD too, above).
OBJ := $(patsubst csrc/%.c,$(BUILD)/%.o,$(SRC))
LIB ?= src/pysacd/_sacdconv.$(LIBEXT)

all: $(LIB)
ifeq ($(FLAC_LIBS),)
	@echo "note: libFLAC not found; built without FLAC output (install flac-devel and 'make clean all')"
endif

# An engine without scarletbook's functions must not be built quietly (#225): the export list
# is checked before linking, and the linked engine's export table after it. Either check
# failing stops the build. SB_REQUIRED: the calls pysacd makes first when it loads the engine.
SB_REQUIRED := sb_abi_version sb_version sb_build_info sb_last_error_message sb_disc_toc
ifeq ($(origin NM),undefined)
NM := $(shell $(CC) -print-prog-name=nm 2>/dev/null || echo nm)
endif
ifeq ($(origin OBJDUMP),undefined)
OBJDUMP := $(shell $(CC) -print-prog-name=objdump 2>/dev/null || echo objdump)
endif
ifeq ($(OS),Windows_NT)
override OBJDUMP := $(subst \,/,$(OBJDUMP))
endif
SB_SYMCMD = $(if $(filter objdump,$(SB_SYMTOOL)),$(OBJDUMP),$(NM)) $(call SB_SYMS,$(1))
# $(call SB_CHECK_DEF,FILE): a Windows exports.def (generated from the header) names every
# SB_REQUIRED function. $(call SB_CHECK_LIST,FILE): the platform's export list does; Linux and
# macOS export scarletbook's functions by one wildcard line in a fixed file
define SB_CHECK_DEF
@for s in $(SB_REQUIRED); do grep -qx "    $$s" "$(1)" || { \
	  echo "error: the export list $(1) lacks $$s: the engine would be built without the" \
	       "scarletbook library. Check SCARLETBOOK_DIR=$(SCARLETBOOK_DIR) (#225)" >&2; exit 1; }; done
endef
define SB_CHECK_MAP
@grep -q '$(SB_EXPORTS_HAS)' "$(1)" || { echo "error: the export list $(1) has no line" \
	  "for scarletbook's functions; the engine would be built without them (#225)" >&2; exit 1; }
endef
SB_CHECK_LIST = $(if $(SB_EXPORTS_HAS),$(SB_CHECK_MAP),$(SB_CHECK_DEF))
# $(call SB_CHECK_ENGINE,LIB): the built engine exports every SB_REQUIRED function; removed if not
define SB_CHECK_ENGINE
@syms=$$($(call SB_SYMCMD,$(1))) || { echo "error: can't list the symbols of $(1) with $(SB_SYMTOOL)" \
	"(set NM= or OBJDUMP= to the tool for this compiler)" >&2; rm -f "$(1)"; exit 1; }; \
	for s in $(SB_REQUIRED); do \
	  printf '%s\n' "$$syms" | grep -Eq "(^|[^A-Za-z0-9_])_?$$s([^A-Za-z0-9_]|$$)" || { \
	    echo "error: $(1) does not export $$s: it was built without the scarletbook library." \
	         "Check SCARLETBOOK_DIR=$(SCARLETBOOK_DIR) (#225)" >&2; rm -f "$(1)"; exit 1; }; \
	done
endef

$(LIB): $(OBJ) $(D2P_OBJ) $(SB_READY) $(SB_EXPORTS_DEP)
	$(call SB_CHECK_LIST,$(SB_EXPORTS_DEP))
	$(CC) $(SHARED) $(CFLAGS) $(LDFLAGS) $(SB_EXPORTS) $(SB_STRIP) -o $@ $(OBJ) $(D2P_OBJ) \
	    $(call SB_WHOLE,$(SB_LIB)) $(D2P_IMPORTS) $(SB_LIBS) $(LDLIBS)
	$(call SB_CHECK_ENGINE,$@)

# The two checks on their own, for tests/test_scarletbook.py:
#   make check-export-list SB_EXPORT_LIST=FILE      make check-engine LIB=FILE
check-export-list:
	$(call SB_CHECK_LIST,$(SB_EXPORT_LIST))

check-engine:
	$(call SB_CHECK_ENGINE,$(LIB))

# Windows: every function declared in scarletbook.h and every sacdconv_* function defined in
# csrc/sacdconv.c (each declaration starts its line: `type name(` or `type *name(`)
# (a sed that can't read the header must stop the build, not leave scarletbook out: #225)
$(BUILD)/exports.def: $(SB_READY) csrc/sacdconv.c
	@mkdir -p $(dir $@)
	{ echo EXPORTS && \
	  sed -n 's/^[A-Za-z][A-Za-z0-9_ ]*[ *]\(sb_[a-z0-9_]*\)(.*/    \1/p' "$(SCARLETBOOK_DIR)/include/scarletbook.h" && \
	  sed -n 's/^[A-Za-z][A-Za-z0-9_ ]*[ *]\(sacdconv_[a-z0-9_]*\)(.*/    \1/p' csrc/sacdconv.c; } > $@.tmp \
	  || { rm -f $@.tmp; exit 1; }
	$(call SB_CHECK_DEF,$@.tmp)
	mv $@.tmp $@

$(BUILD)/%.o: csrc/%.c $(wildcard csrc/*.h) $(SB_READY) $(D2P_READY)
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
# repository); the engine's own code is instrumented, all of it (its PCM filters, which a coverage
# ignore list kept out until 0.5.5, are dsd2pcm's now, linked as built and fuzzed in dsd2pcm-rs).
# FLAC output is fuzzed too when libFLAC is found (#157); libFLAC itself is linked as installed.
# FUZZ_SAN: the sanitizers (thread for TSan).
FUZZ_CC    ?= clang
FUZZ_SAN   ?= address,undefined
FUZZ_FLAGS := -O1 -g -fno-omit-frame-pointer -fsanitize=fuzzer,$(FUZZ_SAN) \
              $(if $(findstring undefined,$(FUZZ_SAN)),-fno-sanitize-recover=undefined) -std=gnu17 -pthread
FUZZ_SRC   := csrc/sacdconv.c
FUZZ_FLAC_CFLAGS := $(if $(FLAC_LIBS),-DHAVE_FLAC $(FLAC_DEFS) $(FLAC_CFLAGS))
# The same harness without libFuzzer, for any compiler: tests/test_fuzz_engine.py replays the
# regression inputs and seeds with it (REPLAY_FLAGS: the sanitizer CI jobs' flags), with FLAC output
# when libFLAC is found
REPLAY_FLAGS ?= -O1 -g

fuzz: build/fuzz_engine

build/fuzz_engine: fuzz/fuzz_engine.c $(FUZZ_SRC) $(wildcard csrc/*.h) $(SB_READY) $(D2P_OBJ)
	@mkdir -p $(dir $@)
	$(FUZZ_CC) $(FUZZ_FLAGS) $(FUZZ_FLAC_CFLAGS) -I$(SCARLETBOOK_DIR)/include -I$(DSD2PCM_DIR)/include -o $@ $< $(FUZZ_SRC) $(D2P_OBJ) $(SB_LIB) $(SB_LIBS) \
	    $(FLAC_LIBS) -lm

fuzz-replay: build/replay_engine

build/replay_engine: fuzz/fuzz_engine.c fuzz/replay.c $(FUZZ_SRC) $(wildcard csrc/*.h) $(SB_READY) $(D2P_OBJ)
	@mkdir -p $(dir $@)
	$(CC) $(REPLAY_FLAGS) -std=gnu17 -pthread $(FUZZ_FLAC_CFLAGS) -I$(SCARLETBOOK_DIR)/include -I$(DSD2PCM_DIR)/include -o $@ \
	    fuzz/fuzz_engine.c fuzz/replay.c $(FUZZ_SRC) $(D2P_OBJ) $(SB_LIB) $(SB_LIBS) $(FLAC_LIBS) -lm

# A cancel from another thread wakes the engine's waiting threads (tests/test_cancel_wakeup.py): the
# engine with its test hooks (SC_TEST_HOOKS), which the library pysacd loads never has
cancel-wakeup: build/cancel_wakeup

build/cancel_wakeup: tests/native/cancel_wakeup.c $(FUZZ_SRC) $(wildcard csrc/*.h) $(SB_READY) $(D2P_OBJ)
	@mkdir -p $(dir $@)
	$(CC) $(REPLAY_FLAGS) -std=gnu17 -pthread -DSC_TEST_HOOKS -I$(SCARLETBOOK_DIR)/include -I$(DSD2PCM_DIR)/include -o $@ \
	    tests/native/cancel_wakeup.c $(FUZZ_SRC) $(D2P_OBJ) $(SB_LIB) $(SB_LIBS) -lm

# The engine's leak check (#122): the same harness with FLAC output added, and no interpreter
# whose own allocations would hide the engine's. LEAK_FLAGS: -fsanitize=leak, or -g for valgrind.
LEAK_FLAGS ?= -O1 -g -fno-omit-frame-pointer

leak-driver: build/leak_engine

build/leak_engine: fuzz/fuzz_engine.c fuzz/replay.c $(FUZZ_SRC) $(wildcard csrc/*.h) $(SB_READY) $(D2P_OBJ)
	@test -n "$(FLAC_LIBS)" || { echo "the leak check covers FLAC output: install libFLAC's development files" >&2; exit 1; }
	@mkdir -p $(dir $@)
	$(CC) $(LEAK_FLAGS) -std=gnu17 -pthread -DHAVE_FLAC $(FLAC_CFLAGS) -I$(SCARLETBOOK_DIR)/include -I$(DSD2PCM_DIR)/include -o $@ \
	    fuzz/fuzz_engine.c fuzz/replay.c $(FUZZ_SRC) $(D2P_OBJ) $(SB_LIB) $(SB_LIBS) $(FLAC_LIBS) -lm

# `make clean` keeps the downloaded libraries (build/scarletbook, build/dsd2pcm); distclean removes them too.
clean:
	find build -mindepth 1 -maxdepth 1 ! -name scarletbook ! -name dsd2pcm -exec rm -rf {} + 2>/dev/null || true
	rm -rf src/pysacd/_sacdconv.so src/pysacd/_sacdconv.dylib src/pysacd/_sacdconv.dll

distclean: clean
	rm -rf build

.PHONY: all clean distclean test-tools fuzz fuzz-replay cancel-wakeup leak-driver check-export-list check-engine
