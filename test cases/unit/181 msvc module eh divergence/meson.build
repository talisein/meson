project('msvc module eh divergence', 'cpp', default_options: ['cpp_std=c++20'])

# prog only diverges from modlib in cpp_eh (/EHs-c-): a wrongly shared BMI
# would carry the provider's _CPPUNWIND view, which the constexpr probe in
# modlib.ixx/main.cpp turns into a wrong exit code.
modlib = static_library('modlib', 'modlib.ixx')
prog = executable('prog', 'main.cpp', link_with: modlib,
                  override_options: ['cpp_eh=none'])
test('prog', prog)
