#!/usr/bin/env python3
# Emit a real generated module interface plus a header carried in the same
# output list, so a module kwarg can name either one.
import os
import sys

outdir = sys.argv[1]

with open(os.path.join(outdir, 'gen_iface.cppm'), 'w') as f:
    f.write('export module gen_iface;\n')
    f.write('export int gen_value() { return 0; }\n')

with open(os.path.join(outdir, 'gen_config.h'), 'w') as f:
    f.write('#pragma once\n')
    f.write('#define GEN_CONFIG_VALUE 0\n')
