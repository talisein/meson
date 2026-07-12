project('private module variant import diagnostic', 'cpp', default_options: ['cpp_std=c++20'])

mode = get_option('mode')

if mode == 'own'
  # api is public but its own interface imports mylib's own private module
  # priv -- legal within mylib itself (same target, same unkeyed-by-class
  # private dir), but prog23 forces a BMI-only variant of api under a
  # diverging dialect, whose recompile still can't resolve priv (private
  # interfaces are excluded from variant compile sets).
  mylib = static_library('mylib', 'api.cppm', 'priv.cppm',
    cpp_module_interfaces: ['api.cppm'],
    cpp_private_module_interfaces: ['priv.cppm'])

  prog23 = executable('prog23', 'main_own.cpp', link_with: mylib,
                      override_options: ['cpp_std=' + get_option('divergent_std')])
else
  # apia is public but imports libb's private module privb -- illegal even
  # in liba's own compile, and a divergent consumer's variant of apia must
  # fail with the same precise diagnostic, never the generic one, regardless
  # of which collate edge a parallel ninja schedule happens to run first.
  libb = static_library('libb', 'privb.cppm',
    cpp_private_module_interfaces: ['privb.cppm'])
  liba = static_library('liba', 'apia.cppm', link_with: libb,
    cpp_module_interfaces: ['apia.cppm'])

  prog23 = executable('prog23', 'main_dependency.cpp', link_with: liba,
                      override_options: ['cpp_std=' + get_option('divergent_std')])
endif
