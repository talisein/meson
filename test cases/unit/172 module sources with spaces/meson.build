project('module sources with spaces', 'cpp', default_options: ['cpp_std=c++20'])

# A module interface source whose file name contains a space. The BMI is
# named from the module name, but object-derived paths (.ddi, mappers, and
# BMI-only variant outputs) inherit the spaced basename.
modlib = static_library('modlib', 'my mod.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)

# A define-divergent consumer demands a BMI-only variant of the spaced
# interface -- on GCC its path lands in a mapper file, whose format is
# space-separated.
progfoo = executable('progfoo', 'foomain.cpp', link_with: modlib,
                     cpp_args: ['-DFOO'])
test('progfoo', progfoo)
