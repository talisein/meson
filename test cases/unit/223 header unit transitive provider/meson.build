project('header unit transitive provider', 'cpp', default_options: ['cpp_std=c++20'])

# q's interface imports the header unit "h.h". p declares no header units, but
# its interface imports q, so p's BMI carries q's reference to its h.h BMI.
# progfoo defines FOO, declares h.h itself and imports both it and p: one
# translation unit can hold only one BMI of h.h, so progfoo must get BMI-only
# variants of p as well as q, built against its own h.h. Were p's named-module
# BMI class to ignore the define (p itself declares no units), progfoo would
# read p's own BMI and with it a second h.h BMI -- GCC's CRC mismatch, Clang's
# "defined in both". prog shares every BMI with the providers.
q = static_library('q', 'q/q.cppm', cpp_header_units: ['h.h'],
                   include_directories: 'q')
q_dep = declare_dependency(link_with: q, include_directories: 'q')
p = static_library('p', 'p.cppm', dependencies: q_dep)
p_dep = declare_dependency(link_with: p, dependencies: q_dep)

prog = executable('prog', 'main.cpp', dependencies: p_dep,
                  cpp_header_units: ['h.h'])
progfoo = executable('progfoo', 'mainfoo.cpp', dependencies: p_dep,
                     cpp_header_units: ['h.h'], cpp_args: ['-DFOO'])
test('prog', prog)
test('progfoo', progfoo)
