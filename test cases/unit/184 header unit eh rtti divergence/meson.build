project('header unit eh rtti divergence', 'cpp', default_options: ['cpp_std=c++20'])

# prog, prog_eh and prog_rtti each declare their own copy of the same header
# unit; prog_eh diverges only in cpp_eh (-fno-exceptions), prog_rtti only in
# cpp_rtti (-fno-rtti). A wrongly shared unit BMI would carry the wrong
# __cpp_exceptions/__cpp_rtti view, which the constexpr probe turns into a
# wrong exit code.
prog = executable('prog', 'main.cpp', cpp_header_units: ['util.h'])
prog_eh = executable('prog_eh', 'main.cpp', cpp_header_units: ['util.h'],
                     override_options: ['cpp_eh=none'])
prog_rtti = executable('prog_rtti', 'main.cpp', cpp_header_units: ['util.h'],
                       override_options: ['cpp_rtti=false'])
test('prog', prog)
test('prog_eh', prog_eh)
test('prog_rtti', prog_rtti)
