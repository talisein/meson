project('cpp module graph mutation', 'cpp', default_options: ['cpp_std=c++20'])

# Initial state of a graph the unit test mutates across rebuilds (adding,
# renaming and removing an interface source); it rewrites this file with the
# same shape and a different source list each phase.
modlib = static_library('modlib', 'mod.cc', cpp_module_interfaces: ['mod.cc'])
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)
