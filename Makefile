# Builds the native helper src/pysacd/_sacdconv.so (DSF/DSDIFF/WAV always, FLAC if libFLAC is found).
#   make                 build, tuned for this CPU (-march=native)
#   make PORTABLE=1      build for generic x86-64
#   make clean

CC      ?= gcc
ifdef PORTABLE
ARCH    :=
else
ARCH    := -march=native
endif
CFLAGS  ?= -O3 -ffast-math -fno-finite-math-only $(ARCH)
CFLAGS  += -fPIC -std=gnu17 -Wall -pthread
LDLIBS  := -lm -pthread

FLAC_CFLAGS := $(shell pkg-config --cflags flac 2>/dev/null)
FLAC_LIBS   := $(shell pkg-config --libs flac 2>/dev/null)
ifneq ($(FLAC_LIBS),)
CFLAGS  += -DHAVE_FLAC $(FLAC_CFLAGS)
LDLIBS  += $(FLAC_LIBS)
ifeq ($(shell pkg-config --atleast-version=1.5.0 flac && echo y),y)
CFLAGS  += -DHAVE_FLAC_MT
endif
endif

SRC := $(addprefix csrc/,sacdconv.c demux.c dsp.c dstdec.c)
OBJ := $(patsubst csrc/%.c,build/%.o,$(SRC))
LIB := src/pysacd/_sacdconv.so

all: $(LIB)
ifeq ($(FLAC_LIBS),)
	@echo "note: libFLAC not found; built without FLAC output (install flac-devel and 'make clean all')"
endif

$(LIB): $(OBJ)
	$(CC) -shared $(CFLAGS) -o $@ $^ $(LDLIBS)

build/%.o: csrc/%.c $(wildcard csrc/*.h)
	@mkdir -p $(dir $@)
	$(CC) $(CFLAGS) -c -o $@ $<

clean:
	rm -rf build $(LIB)

.PHONY: all clean
