#!/usr/bin/env python3
# Emit a C++20 module translation unit at build time. Every kind below is a
# source Meson only ever sees after this custom_target has run, so the module
# names and imports are discovered by the build-time scan, not from the source
# list at configure time. Some kinds are also named in a module kwarg
# (cpp_internal_partitions / cpp_private_module_interfaces) -- the kwarg reads
# only the output filename, still fixed at configure time, never the contents.
import sys

kind, out = sys.argv[1], sys.argv[2]
args = sys.argv[3:]
# Positional extras, defaulted so building the body table below never indexes
# past the args a given kind was actually invoked with.
val = args[0] if len(args) > 0 else '0'
mod = args[0] if len(args) > 0 else ''
expect = args[1] if len(args) > 1 else '0'

bodies = {
    # Primary interface of module 'pkg': re-exports an interface partition,
    # imports an internal partition (module pkg:detail;, no export) for a
    # definition, and calls another supplied by a separate implementation unit.
    'primary': ('export module pkg;\n'
                'export import :part;\n'
                'import :detail;\n'
                'import :detail2;\n'
                'int impl_value();\n'
                'export int pkg_value() { return part_value() + impl_value() '
                '+ detail_value() + detail2_value() + 1; }\n'),
    # An interface partition, re-exported by the primary above.
    'part': ('export module pkg:part;\n'
             'export int part_value() { return 5; }\n'),
    # Two internal (implementation) partitions of 'pkg' (module pkg:detailN;, no
    # export): BMI-producing module units the primary imports. Their outputs are
    # plain .cpp -- extension detection cannot classify them, and MSVC needs
    # /internalPartition -- so they reach the pipeline only through
    # cpp_internal_partitions, one declared by object form and one by string.
    'detail': ('module pkg:detail;\n'
               'int detail_value() { return 3; }\n'),
    'detail2': ('module pkg:detail2;\n'
                'int detail2_value() { return 2; }\n'),
    # An implementation unit of 'pkg' (module pkg;, no export): provides a
    # definition the primary declared, is ordered after the primary's BMI, and
    # provides no module of its own.
    'implunit': ('module pkg;\n'
                 'int impl_value() { return 10; }\n'),
    # A pure consumer: no module of its own, just an import. It must be scanned
    # after this generator runs, exactly as a hand-written consumer is.
    'consumer': ('import pkg;\n'
                 'int main() { return pkg_value() == 21 ? 0 : 1; }\n'),
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
               f'export int secret_value() {{ return {val}; }}\n'),
    # A private module 'stash' of a library, declared private via
    # cpp_private_module_interfaces. Two libraries generate this same module
    # name with different values; privacy keeps them apart in the shared BMI
    # cache exactly as the private-by-construction executables above.
    'privmod': ('export module stash;\n'
                f'export int stash_value() {{ return {val}; }}\n'),
    # A library's public interface (a plain .cppm, an interface by extension):
    # it declares its function but imports nothing private, so no importer's
    # interface reaches the private module. args[0] is the module name.
    'facepub': (f'export module {mod};\n'
                f'export int {mod}_value();\n'),
    # The public interface's implementation unit: it, not the interface, imports
    # the target's private module 'stash' and defines the exported function.
    'faceimpl': (f'module {mod};\n'
                 'import stash;\n'
                 f'int {mod}_value() {{ return stash_value() + 1; }}\n'),
    # A consumer importing a library's public interface across the link. args[0]
    # is the module name, args[1] the value it must observe.
    'facemain': (f'import {mod};\n'
                 f'int main() {{ return {mod}_value() == {expect} ? 0 : 1; }}\n'),
}

with open(out, 'w', encoding='utf-8') as f:
    f.write(bodies[kind])
