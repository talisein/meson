project('msvc module internal partitions', 'cpp', default_options: ['cpp_std=c++20'])

# An internal (implementation) partition: `module pkg:impl;` -- no export, but
# an importable BMI, consumed by the primary interface. On MSVC it must be a
# plain .cpp (cl rejects an interface extension with /internalPartition) and is
# declared via cpp_internal_partitions. The partition chain :impl -> :part also
# exercises partition-to-partition requires.
pkglib = static_library('pkg', 'pkg.ixx', 'pkg-part.ixx', 'pkg-impl.cpp',
  cpp_internal_partitions: ['pkg-impl.cpp'])
prog = executable('prog', 'main.cpp', link_with: pkglib)
test('prog', prog)

# An RTTI-divergent consumer (a dialect flag, which splits a named module's
# BMI class): its BMI-only variant of pkg must recompile the internal
# partition too -- the constexpr the consumers observe lives in :impl,
# behind two imports.
prognortti = executable('prognortti', 'norttimain.cpp', link_with: pkglib,
                        override_options: ['cpp_rtti=false'])
test('prognortti', prognortti)
