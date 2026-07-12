project('header unit define divergence', 'cpp', default_options: ['cpp_std=c++20'])

# The subproject builds a module library whose interface imports the header
# unit "util.h". prog and progb share its BMI class and must reuse its unit BMI;
# progfoo diverges on -DFOO and must get a unit BMI of its own -- and modlib's
# BMI-only variant for progfoo must import that one, not the provider's.
# util_foo() observes the define and is constant-evaluated by every importer, so
# a wrongly shared BMI is a wrong exit code, not just a build failure.
#
# The classes differ by a define, not a dialect: GCC rejects a dialect-divergent
# unit while scanning, which no per-class BMI can fix (fixture 168).
modlib = subproject('modlib')
modlib_dep = modlib.get_variable('modlib_dep')

prog = executable('prog', 'main.cpp', dependencies: modlib_dep,
                  cpp_header_units: ['util.h'])
progb = executable('progb', 'mainb.cpp', dependencies: modlib_dep,
                   cpp_header_units: ['util.h'])
progfoo = executable('progfoo', 'mainfoo.cpp', dependencies: modlib_dep,
                     cpp_header_units: ['util.h'], cpp_args: ['-DFOO'])
test('prog', prog)
test('progb', progb)
test('progfoo', progfoo)
