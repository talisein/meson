project('clang header units', 'cpp', default_options: ['cpp_std=c++20'])

# A header unit ("util.h") and a named module ("mod") in one target. The header
# unit is declared with cpp_header_units so Meson pre-builds its BMI and passes
# each consumer (and its scan) an explicit -fmodule-file mapping -- Clang has
# no ambient header-unit lookup at all. The named module rides the normal
# scan/collate/dyndep path unchanged.
prog = executable('prog', 'main.cpp', 'mod.cppm',
                  cpp_header_units: ['util.h', '<angleutil.h>'])
test('prog', prog)
