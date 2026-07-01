project('fortran links gcc cpp module', 'cpp', 'fortran',
        default_options: ['cpp_std=c++20'])

# A C++ static library that uses named modules internally: mathmod.cppm is the
# module interface, and capi.cpp imports it and exposes an extern "C" entry
# point. This is a genuine P1689-C++-module target.
cpplib = static_library('mathmod', 'mathmod.cppm', 'capi.cpp')

# A Fortran program that actually calls into that modules-using C++ library via
# ISO_C_BINDING -- real interop, not just an empty link. The Fortran target
# takes the legacy regex-scan dyndep path; linking a P1689 C++-module library
# used to add that library's depscan.json (which the P1689 pipeline never
# emits) as a depaccumulate input, so ninja failed with a missing input.
prog = executable('prog', 'main.f90', link_with: cpplib)
test('prog', prog)
