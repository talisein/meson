project('gcc modules cross machine', 'cpp', default_options: ['cpp_std=c++20'])

# A C++-module target on each machine. Under cross-compilation both would write
# the single shared gcm.cache/ at the build root with BMIs from two different
# compilers, so Meson must error at configure time rather than silently mix them.
static_library('hostmod', 'mod.cppm')
static_library('buildmod', 'mod.cppm', native: true)
