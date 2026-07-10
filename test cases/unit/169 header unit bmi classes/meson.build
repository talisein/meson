project('header unit bmi classes', 'cpp', default_options: ['cpp_std=c++23'])

# The subproject builds a module library at c++20 whose interface imports the
# header unit "util.h". prog20/prog20b share that BMI class and must reuse its
# unit BMI (one edge for all three declarers in the class); prog23 diverges
# and must get a class-mate unit BMI of its own -- and modlib's BMI-only
# variant for prog23 must import that one, not the provider's. util_cpp()
# observes the dialect and is constant-evaluated by every importer, so a
# wrongly shared BMI is a wrong exit code, not just a build failure.
modlib = subproject('modlib')
modlib_dep = modlib.get_variable('modlib_dep')
prog23 = executable('prog23', 'main23.cpp', dependencies: modlib_dep,
                    cpp_header_units: ['util.h'])
prog20 = executable('prog20', 'main20.cpp', dependencies: modlib_dep,
                    cpp_header_units: ['util.h'],
                    override_options: ['cpp_std=c++20'])
prog20b = executable('prog20b', 'main20b.cpp', dependencies: modlib_dep,
                     cpp_header_units: ['util.h'],
                     override_options: ['cpp_std=c++20'])
test('prog23', prog23)
test('prog20', prog20)
test('prog20b', prog20b)
