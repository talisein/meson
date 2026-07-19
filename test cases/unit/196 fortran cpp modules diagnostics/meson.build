project('fortran cpp modules mix', 'cpp', 'fortran',
        default_options: ['cpp_std=c++20'])

# Three ways a Fortran and C++ named modules meet in one build, all supported:
# a target runs both module scanners at once, its C++ TUs importing modules
# whether the interface is in the same target or in a linked library. Selected
# by -Dmode= so one project drives every case from the unit test.
mode = get_option('mode')

if mode == 'module_source'
  # A C++ module interface directly in the mixed target: mathmod.cppm is
  # compiled as a module, capi.cpp imports it, and the Fortran main calls in
  # through the extern "C" boundary.
  test('prog', executable('prog', 'main.f90', 'mathmod.cppm', 'capi.cpp'))
elif mode == 'links_provider'
  # The module interface lives in a linked library; the mixed target's own C++
  # TU imports it across the link.
  cpplib = static_library('mathmod', 'mathmod.cppm')
  test('prog', executable('prog', 'main.f90', 'shim.cpp', link_with: cpplib))
else
  # The provider carries the module and its C-linkage entry; the Fortran target
  # merely links it and has no C++ TU of its own.
  cpplib = static_library('mathmod', 'mathmod.cppm', 'capi.cpp')
  test('prog', executable('prog', 'main.f90', link_with: cpplib))
endif
