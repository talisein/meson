project('cpp private module interfaces diagnostics', 'cpp', default_options: ['cpp_std=c++20'])

mode = get_option('mode')

if mode == 'both-kwargs'
  # Configure-time error: a source can't be listed in both
  # cpp_module_interfaces and cpp_private_module_interfaces.
  static_library('bad', 'detail.cppm',
    cpp_module_interfaces: ['detail.cppm'],
    cpp_private_module_interfaces: ['detail.cppm'])
else
  mylib = static_library('mylib', 'detail.cppm',
    cpp_private_module_interfaces: ['detail.cppm'])

  if mode == 'direct-import'
    # A target outside mylib imports mylib's private module directly.
    executable('prog', 'direct_import_main.cpp', link_with: mylib)
  elif mode == 'missing'
    # A module nowhere in the build: the private-module check above must not
    # swallow this into a misleading "provided privately" diagnostic.
    executable('prog', 'missing_main.cpp', link_with: mylib)
  elif mode == 'cycle'
    # Two private modules importing each other: a cycle entirely among
    # private names must still be caught, exactly as for public ones.
    cyclib = static_library('cyclib', 'cyc_a.cppm', 'cyc_b.cppm',
      cpp_private_module_interfaces: ['cyc_a.cppm', 'cyc_b.cppm'])
    executable('prog', 'cycle_main.cpp', link_with: cyclib)
  endif
endif
