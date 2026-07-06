#!/usr/bin/env python3
# Emit a C++20 module interface at build time. The module name is only known
# after this runs -- Meson discovers it via the build-time scan.
import sys

with open(sys.argv[1], 'w', encoding='utf-8') as f:
    f.write('export module genmod;\n')
    f.write('export int gen_value() { return 123; }\n')
