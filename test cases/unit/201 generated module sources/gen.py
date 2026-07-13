#!/usr/bin/env python3
# Emit a C++20 module translation unit at build time. Every kind below is a
# source Meson only ever sees after this custom_target has run, so the module
# names and imports are discovered by the build-time scan, not from the source
# list at configure time.
import sys

kind, out = sys.argv[1], sys.argv[2]
value = sys.argv[3] if len(sys.argv) > 3 else '0'

bodies = {
    # Primary interface of module 'pkg': re-exports an interface partition and
    # calls a definition supplied by a separate implementation unit.
    'primary': ('export module pkg;\n'
                'export import :part;\n'
                'int impl_value();\n'
                'export int pkg_value() { return part_value() + impl_value() + 1; }\n'),
    # An interface partition, re-exported by the primary above.
    'part': ('export module pkg:part;\n'
             'export int part_value() { return 5; }\n'),
    # An implementation unit of 'pkg' (module pkg;, no export): provides a
    # definition the primary declared, is ordered after the primary's BMI, and
    # provides no module of its own.
    'implunit': ('module pkg;\n'
                 'int impl_value() { return 10; }\n'),
    # A pure consumer: no module of its own, just an import. It must be scanned
    # after this generator runs, exactly as a hand-written consumer is.
    'consumer': ('import pkg;\n'
                 'int main() { return pkg_value() == 16 ? 0 : 1; }\n'),
    # An interface that itself imports another module and re-exports on top of
    # it: a generated interface in the consumer role as well as the provider one.
    'wrapiface': ('export module wrap;\n'
                  'import pkg;\n'
                  'export int wrap_value() { return pkg_value() * 2; }\n'),
    # A module private by construction: it lives in an executable nothing can
    # link, so nothing outside that executable can import it. Two executables
    # generate this same module name with different values; the build must keep
    # them apart in the shared BMI cache.
    'secret': ('export module secret;\n'
               f'export int secret_value() {{ return {value}; }}\n'),
}

with open(out, 'w', encoding='utf-8') as f:
    f.write(bodies[kind])
