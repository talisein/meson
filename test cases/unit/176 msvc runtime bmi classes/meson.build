project('msvc runtime bmi classes', 'cpp',
        default_options: ['cpp_std=c++20', 'b_vscrt=md'])

# Two executables share one module provider but select different MSVC runtime
# libraries: /MD (dynamic) and /MT (static). The runtime flag is BMI-affecting
# and not in the allowlist, so it splits the BMI class -- prog_mt resolves
# modlib against a BMI-only variant compiled /MT, while still linking the
# provider's (runtime-neutral) object. prog_md shares the provider's own /MD
# BMI, spawning no variant.
modlib = static_library('modlib', 'modlib.ixx')
prog_md = executable('prog_md', 'main.cpp', link_with: modlib)
prog_mt = executable('prog_mt', 'main.cpp', link_with: modlib,
                     override_options: ['b_vscrt=mt'])
test('prog_md', prog_md)
test('prog_mt', prog_mt)
