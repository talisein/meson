project('module eh rtti divergence', 'cpp', default_options: ['cpp_std=c++20'])

# prog shares modlib's class; prog_eh diverges only in cpp_eh
# (-fno-exceptions), prog_rtti only in cpp_rtti (-fno-rtti). Neither flag is
# on Clang's or GCC's BMI-irrelevant allowlist, so each must resolve modlib
# through its own BMI-only variant. A wrongly shared BMI would carry the
# provider's __cpp_exceptions/__cpp_rtti view, which the constexpr probes in
# modlib.cppm/main.cpp turn into a wrong exit code.
modlib = static_library('modlib', 'modlib.cppm')
prog = executable('prog', 'main.cpp', link_with: modlib)
prog_eh = executable('prog_eh', 'main.cpp', link_with: modlib,
                     override_options: ['cpp_eh=none'])
prog_rtti = executable('prog_rtti', 'main.cpp', link_with: modlib,
                       override_options: ['cpp_rtti=false'])
test('prog', prog)
test('prog_eh', prog_eh)
test('prog_rtti', prog_rtti)
