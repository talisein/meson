project('module internal partition diagnostics', 'cpp', default_options: ['cpp_std=c++20'])

# Configure-time error: a source can't be listed in both cpp_module_interfaces
# and cpp_internal_partitions -- it is an interface unit or an internal
# partition, not both. Nothing here ever compiles.
static_library('bad', 'pkg.cppm', 'pkg-impl.cpp',
  cpp_module_interfaces: ['pkg.cppm', 'pkg-impl.cpp'],
  cpp_internal_partitions: ['pkg-impl.cpp'])
