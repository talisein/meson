project('duplicate module names', 'cpp', default_options: ['cpp_std=c++20'])

mode = get_option('mode')

if mode == 'bothlibs'
  # One both_libraries() whose static and shared halves compile the interface
  # separately (b_staticpic=false keeps the static objects non-PIC, so they
  # cannot be reused) yet in one BMI class: two providers of one module.
  lib = both_libraries('dup', 'duplicate.cppm')
  test('usestatic', executable('usestatic', 'main.cpp', link_with: lib.get_static_lib()))
  test('useshared', executable('useshared', 'main.cpp', link_with: lib.get_shared_lib()))
else
  # Two unrelated libraries built from one interface source with the same
  # flags, so in one BMI class, each providing the module "duplicate". No link
  # reaches both, so both are legal providers, and each consumer resolves the
  # one it links.
  dup1 = static_library('dup1', 'duplicate.cppm')
  dup2 = static_library('dup2', 'duplicate.cppm')
  test('use1', executable('use1', 'main.cpp', link_with: dup1))
  test('use2', executable('use2', 'main.cpp', link_with: dup2))
  if mode == 'linkboth'
    # One link reaching both providers is still an error.
    executable('useboth', 'main.cpp', link_with: [dup1, dup2])
  endif
endif
