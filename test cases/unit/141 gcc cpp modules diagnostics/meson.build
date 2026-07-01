project('gcc cpp modules diagnostics', 'cpp', default_options: ['cpp_std=c++20'])

# Each mode configures cleanly but must FAIL at build time in the collator with
# a specific module diagnostic. Selected by -Dmode= so one project drives all
# cases from the unit test.
mode = get_option('mode')

if mode == 'missing'
  # A source imports a module that no target provides.
  executable('p', 'importsmissing.cpp', cpp_modules: true)
elif mode == 'cycle'
  # Two modules import each other.
  executable('p', 'main.cpp', 'cyca.cppm', 'cycb.cppm')
elif mode == 'duplicate'
  # Two sources in one target export the same module.
  executable('p', 'main.cpp', 'dupa.cppm', 'dupb.cppm')
elif mode == 'crosslink'
  # The executable and a library it links both provide the same module.
  lib = static_library('provlib', 'libprov.cppm')
  executable('p', 'main.cpp', 'exeprov.cppm', link_with: lib)
elif mode == 'duptargets'
  # Two unrelated targets each export the same module: their BMIs would
  # collide in the shared module cache.
  static_library('liba', 'dupa.cppm')
  static_library('libb', 'dupb.cppm')
endif
