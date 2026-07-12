project('msvc module rtti divergence', 'cpp', default_options: ['cpp_std=c++20'])

# prog only diverges from modlib in cpp_rtti (/GR-, which cl's class key does
# not strip): a wrongly shared BMI would carry the provider's RTTI view, which
# the constexpr probe in modlib.ixx/main.cpp turns into a wrong exit code.
modlib = static_library('modlib', 'modlib.ixx')
prog = executable('prog', 'main.cpp', link_with: modlib,
                  override_options: ['cpp_rtti=false'])
test('prog', prog)
