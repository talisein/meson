project('gcc cpp modules', 'cpp', default_options: ['cpp_std=c++20'])

# A library that provides a named module, imported by an executable that only
# links the library. Nothing about modlib's sources is named in the exe -- the
# provided-module map + dyndep carry ordering and resolution. The exe is
# module-enabled purely by linking a module provider, with no annotation.
modlib = static_library('modlib', 'modlib.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)

# Module partitions: pkg:part must land in gcm.cache/pkg-part.gcm and be built
# before the primary interface that re-exports it.
pkglib = static_library('pkg', 'pkg.cppm', 'pkg-part.cppm')
pkgprog = executable('pkgprog', 'pkgmain.cpp', link_with: pkglib)
test('pkgprog', pkgprog)

# Explicit opt-in: a target with no module-interface source of its own, enabled
# by the cpp_modules kwarg, still gets scanned so its import is ordered.
kwlib = static_library('kwlib', 'kwmod.cppm')
kwprog = executable('kwprog', 'kwmain.cpp', link_with: kwlib, cpp_modules: true)
test('kwprog', kwprog)

# Generated module interface: the .cppm is produced at build time; its
# declared output name enables the module machinery, and its module name is
# discovered by the build-time scan. Consumed across a target boundary.
py = find_program('python3')
genmod = custom_target('genmod',
  output: 'genmod.cppm',
  command: [py, files('gen_module.py'), '@OUTPUT@'])
genlib = static_library('genlib', genmod)
genprog = executable('genprog', 'genmain.cpp', link_with: genlib)
test('genprog', genprog)
