project('fortran cpp modules cross link', 'cpp', 'fortran',
        default_options: ['cpp_std=c++20'])

# A mixed provider library exporting both a C++ module (mathmod) and a Fortran
# module (fmod).
mixedlib = static_library('mixedlib', 'mathmod.cppm', 'fmod.f90')

# A second mixed target linking it: consumer.cpp imports the C++ module and
# main.f90 uses the Fortran module, both resolved across the link. This pins the
# consumer-side collection: the provider is itself on the P1689 pipeline, so a
# guard asking "which C++ pipeline is the provider on" would silently stop
# collecting its Fortran depscan and break Fortran resolution across the link.
prog = executable('prog', 'main.f90', 'consumer.cpp', link_with: mixedlib)
test('prog', prog)
