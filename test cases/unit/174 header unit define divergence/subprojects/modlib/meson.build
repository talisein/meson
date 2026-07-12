project('modlib', 'cpp', default_options: ['cpp_std=c++20'])

modlib_lib = static_library('modlib', 'modlib.cppm', cpp_header_units: ['util.h'])
modlib_dep = declare_dependency(link_with: modlib_lib, include_directories: '.')
