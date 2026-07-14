project('header unit dialect divergence', 'cpp', default_options: ['cpp_std=c++20'])

# Three declarers of the same header unit across a dialect divergence: prog20
# and prog20b share one BMI class (c++20), prog23 diverges (c++23). GCC checks
# a header unit's dialect when it reads the CMI -- the P1689 scan included -- so
# each class must get its own BMI, reached through its own class root, or a scan
# hard-errors on the mismatched dialect. util_std_view() bakes the dialect into
# each class's BMI, and every program constant-evaluates it against its own
# view, so a wrongly shared BMI is a failing test run, not merely a build error.
prog20 = executable('prog20', 'main20.cpp', cpp_header_units: ['util.h'])
prog20b = executable('prog20b', 'main20b.cpp', cpp_header_units: ['util.h'])
prog23 = executable('prog23', 'main23.cpp', cpp_header_units: ['util.h'],
                    override_options: ['cpp_std=c++23'])
test('prog20', prog20)
test('prog20b', prog20b)
test('prog23', prog23)
