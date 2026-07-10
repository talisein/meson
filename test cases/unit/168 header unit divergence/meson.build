project('header unit divergence', 'cpp', default_options: ['cpp_std=c++20'])

# Three declarers of the same header unit: prog20 builds its BMI, prog20b
# reuses it from the same BMI class (quiet), prog23 reuses it from a
# divergent class and must draw exactly one warning.
prog20 = executable('prog20', 'main20.cpp', cpp_header_units: ['util.h'])
prog20b = executable('prog20b', 'main20b.cpp', cpp_header_units: ['util.h'])
prog23 = executable('prog23', 'main23.cpp', cpp_header_units: ['util.h'],
                    override_options: ['cpp_std=c++23'])
test('prog20', prog20)
test('prog20b', prog20b)
test('prog23', prog23)
