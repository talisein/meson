project('msvc header unit eh divergence', 'cpp', default_options: ['cpp_std=c++20'])

# prog_a and prog_b each declare their own copy of the same header unit;
# prog_b diverges only in cpp_eh (/EHs-c-). A wrongly shared unit BMI would
# carry the wrong _CPPUNWIND view, which the constexpr probe turns into a
# wrong exit code.
prog_a = executable('prog_a', 'main.cpp', cpp_header_units: ['util.h'])
prog_b = executable('prog_b', 'main.cpp', cpp_header_units: ['util.h'],
                    override_options: ['cpp_eh=none'])
test('prog_a', prog_a)
test('prog_b', prog_b)
