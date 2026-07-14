project('system header unit dialect divergence', 'cpp',
        default_options: ['cpp_std=c++20'])

# Two classes both importing the same *system* header unit (import <vector>;)
# across a dialect divergence: prog20 stays at the c++20 default, prog23
# diverges to c++23. A system unit is named through the compiler's own built-in
# include chain, not through any -I, so its per-class BMI is reached by aliasing
# that whole chain on the scan. GCC checks a header unit's dialect when it reads
# the CMI, the P1689 scan included, so a scan reaching the other class's <vector>
# BMI would hard-error on the mismatch: each class must get its own. Every
# program constant-evaluates its own compile's __cplusplus, so a wrongly shared
# unit is a failing run rather than only a build error.
#
# prog20 also declares <cstdint>, a system unit only its class imports (in a TU
# of its own -- never two system units in one TU, which MinGW's libstdc++ cannot
# take alongside a C-runtime one). The chain aliases are per target, not per
# unit: one scan resolves every system header through the aliased chain or
# none, so the single-declarer unit rides the same class-aliased naming as the
# shared one.
prog20 = executable('prog20', 'main20.cpp', 'use20.cpp',
                    cpp_header_units: ['<vector>', '<cstdint>'])
prog23 = executable('prog23', 'main23.cpp', cpp_header_units: ['<vector>'],
                    override_options: ['cpp_std=c++23'])
test('prog20', prog20)
test('prog23', prog23)
