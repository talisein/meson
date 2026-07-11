project('clang cpp modules', 'cpp', default_options: ['cpp_std=c++20'])

# Mirror of "138 gcc cpp modules" for the Clang pipeline (clang-scan-deps
# P1689 scan, source-keyed BMIs harvested into pcm.cache).

# A library that provides a named module, imported by an executable that only
# links the library; the exe is module-enabled purely by linking a provider.
# The library also has a module implementation unit (plain .cpp,
# `module modlib;`): ordered after the interface's BMI, provides none, and
# on clang gets no -fmodule-output/harvest (it is not an interface).
modlib = static_library('modlib', 'modlib.cppm', 'modlib_impl.cpp')
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)

# Module partitions: pkg:part must land in pcm.cache/pkg-part.pcm and be built
# before the primary interface that re-exports it.
pkglib = static_library('pkg', 'pkg.cppm', 'pkg-part.cppm')
pkgprog = executable('pkgprog', 'pkgmain.cpp', link_with: pkglib)
test('pkgprog', pkgprog)

# Explicit opt-in: a target with no module-interface source of its own, enabled
# by the cpp_modules kwarg, still gets scanned so its import is ordered.
kwlib = static_library('kwlib', 'kwmod.cppm')
kwprog = executable('kwprog', 'kwmain.cpp', link_with: kwlib, cpp_modules: true)
test('kwprog', kwprog)

# Generated module interface: the .cppm is produced at build time; its module
# name is discovered by the build-time scan.
py = find_program('python3')
genmod = custom_target('genmod',
  output: 'genmod.cppm',
  command: [py, files('gen_module.py'), '@OUTPUT@'])
genlib = static_library('genlib', genmod)
genprog = executable('genprog', 'genmain.cpp', link_with: genlib)
test('genprog', genprog)

# Module name != file name: the harvest step must name the cache BMI from the
# scanned module name (oddname), not the source file name (oddfile).
oddlib = static_library('oddlib', 'oddfile.cppm')
oddprog = executable('oddprog', 'oddmain.cpp', link_with: oddlib)
test('oddprog', oddprog)

# Dotted module names: '.' is a plain name character, not a partition
# separator, so dot.mod.sub must map to pcm.cache/dot.mod.sub.pcm unchanged.
dotlib = static_library('dotlib', 'dotted.cppm')
dotprog = executable('dotprog', 'dotmain.cpp', link_with: dotlib)
test('dotprog', dotprog)
