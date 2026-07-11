project('modlib', 'cpp', default_options: ['cpp_std=c++20'])

# The second, unimported interface exists so incremental tests can edit one
# interface and assert the provider's other TU stays untouched.
modlib_lib = static_library('modlib', 'modlib.cppm', 'util.cppm')
modlib_dep = declare_dependency(link_with: modlib_lib)
