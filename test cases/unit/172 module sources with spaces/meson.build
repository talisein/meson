project('module sources with spaces', 'cpp', default_options: ['cpp_std=c++20'])

# A module interface source whose file name contains a space. The BMI is
# named from the module name, but object-derived paths (.ddi, mapper tables,
# and BMI-only variant outputs) inherit the spaced basename.
modlib = static_library('modlib', 'my mod.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)

# An RTTI-divergent consumer (a dialect flag, which splits a named module's
# BMI class) demands a BMI-only variant of the spaced interface -- on GCC its
# path lands in a mapper table, and before GCC 16 that table cannot be named
# by a spaced path on the mapper's command line.
prognortti = executable('prognortti', 'norttimain.cpp', link_with: modlib,
                        override_options: ['cpp_rtti=false'])
test('prognortti', prognortti)
