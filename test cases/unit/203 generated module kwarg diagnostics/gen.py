#!/usr/bin/env python3
# Emit a trivial C++20 module interface. These cases all fail at configure
# time -- the module kwargs are validated before anything is built -- so the
# contents are never compiled; only the output filename is ever read.
import sys

with open(sys.argv[1], 'w', encoding='utf-8') as f:
    f.write('export module dup;\nexport int dup_value() { return 0; }\n')
