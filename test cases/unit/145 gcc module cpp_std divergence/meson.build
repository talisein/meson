project('gcc module cpp_std divergence', 'cpp', default_options: ['cpp_std=c++20'])

# The module provider is built at the project default (c++20); the consumer
# overrides to c++23. Because C++ module BMIs live in one shared cache and are
# not std-versioned, Meson must warn that the two share modules under divergent
# cpp_std rather than silently (mis)building.
modlib = static_library('modlib', 'modlib.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib,
                  override_options: ['cpp_std=c++23'])
test('prog', prog)
