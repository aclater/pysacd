/*
 * Run a fuzz harness once per file named on the command line, without
 * libFuzzer, so any compiler (and the sanitizer CI job) can replay the
 * regression inputs in fuzz/regressions/.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size);

int main(int argc, char **argv)
{
    for (int i = 1; i < argc; i++) {
        FILE *f = fopen(argv[i], "rb");
        if (!f) {
            perror(argv[i]);
            return 1;
        }
        uint8_t *buf = NULL;
        size_t len = 0, cap = 0, n;
        do {
            if (len == cap && !(buf = realloc(buf, cap = cap ? 2 * cap : 65536))) {
                fclose(f);
                return 1;
            }
            n = fread(buf + len, 1, cap - len, f);
            len += n;
        } while (n);
        fclose(f);
        LLVMFuzzerTestOneInput(buf, len);
        free(buf);
    }
    return 0;
}
