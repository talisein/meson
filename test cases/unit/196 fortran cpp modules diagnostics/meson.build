project('fortran cpp modules diagnostics', 'cpp', 'fortran',
        default_options: ['cpp_std=c++20'])

# A target gets one module scanner, and a target with Fortran sources uses the
# Fortran one -- so its C++ sources are compiled with no module flags and no
# scan. Each mode below is a different way of asking for C++ modules in such a
# target; only 'supported' is a shape Meson can build. Selected by -Dmode= so
# one project drives every case from the unit test.
mode = get_option('mode')

if mode == 'module_source'
  # A C++ module interface directly in the Fortran target: a setup error. Left
  # alone this compiles mathmod.cppm without -fmodules and dies in the compiler.
  executable('prog', 'main.f90', 'mathmod.cppm', 'capi.cpp')
elif mode == 'links_provider'
  # No module source of its own, but it links one and has C++ TUs -- which
  # cannot import the module. A warning: if no C++ source here imports
  # anything, the build is fine, and this one does not.
  cpplib = static_library('mathmod', 'mathmod.cppm', 'capi.cpp')
  test('prog', executable('prog', 'main.f90', 'shim.cpp', link_with: cpplib))
else
  # The shape that works: the C++ modules live in a C++ library, and the
  # Fortran target merely links it. No diagnostic, and the Fortran main calls
  # through into the module.
  cpplib = static_library('mathmod', 'mathmod.cppm', 'capi.cpp')
  test('prog', executable('prog', 'main.f90', link_with: cpplib))
endif
