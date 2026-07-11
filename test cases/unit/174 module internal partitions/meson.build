project('module internal partitions', 'cpp', default_options: ['cpp_std=c++20'])

# An internal (implementation) partition: `module pkg:impl;` -- no export,
# but an importable BMI, consumed by the primary interface. The partition
# chain :impl -> :part also exercises partition-to-partition requires.
# cpp_internal_partitions is accepted here as a portable no-op: GCC infers the
# unit kind and Clang's -x c++-module is kind-agnostic, so the declaration only
# changes a flag on MSVC (see unit test 175).
pkglib = static_library('pkg', 'pkg.cppm', 'pkg-part.cppm', 'pkg-impl.cppm',
  cpp_internal_partitions: ['pkg-impl.cppm'])
prog = executable('prog', 'main.cpp', link_with: pkglib)
test('prog', prog)

# A define-divergent consumer: its BMI-only variant of pkg must recompile
# the internal partition too -- the constexpr the consumers observe lives
# in :impl, behind two imports.
progfoo = executable('progfoo', 'foomain.cpp', link_with: pkglib,
                     cpp_args: ['-DFOO'])
test('progfoo', progfoo)
