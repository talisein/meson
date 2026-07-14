project('modlib', 'cpp', default_options: ['cpp_std=c++20'])

# No include_directories on the dep: nothing outside this subproject can even
# resolve "util.h", which is the point. A consumer inherits the unit's *BMI*,
# and that BMI has to be built on the include path that resolves the spelling
# modlib declared -- modlib's own, never the consumer's.
modlib_lib = static_library('modlib', 'modlib.cppm', cpp_header_units: ['util.h'])
modlib_dep = declare_dependency(link_with: modlib_lib)
