project('fortran cpp modules one target', 'cpp', 'fortran',
        default_options: ['cpp_std=c++20'])

# One target running both module systems in a single build: mathmod.cppm is a
# C++ module interface that capi.cpp imports, and fmod.f90 is a Fortran module
# that main.f90 uses across two .f90 files. The two scanners produce two
# language-keyed dyndep files (depscan-cpp.dd, depscan-fortran.dd), each naming
# only its own language's compile edges, and the Fortran main calls into the
# C++ module through ISO_C_BINDING.
prog = executable('prog', 'main.f90', 'fmod.f90', 'mathmod.cppm', 'capi.cpp')
test('prog', prog)
