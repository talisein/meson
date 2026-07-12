project('cpp private module interfaces', 'cpp', default_options: ['cpp_std=c++20'])

# libdep provides a public module: mylib's own implementation TU imports it
# alongside its own private module, in the same translation unit, so a
# GCC mapper file must mix a --private-bmi-dir entry with a --bmi-dir entry.
libdep = static_library('libdep', 'pub.cppm')

# mylib mixes a public interface (api) with two private ones: detail (a
# plain private module) and hidden (a private module with its own internal
# partition, also independently marked private -- privacy is not inherited
# from the primary, see hidden-impl.cppm). mode=unlisted-partition leaves
# hidden-impl.cppm out of cpp_private_module_interfaces while still declaring
# it a partition of hidden, provoking the partition-privacy diagnostic.
private_ifaces = ['detail.cppm', 'hidden.cppm']
if get_option('mode') == 'ok'
  private_ifaces += ['hidden-impl.cppm']
endif
mylib = static_library('mylib', 'api.cppm', 'detail.cppm', 'hidden.cppm', 'hidden-impl.cppm', 'api_impl.cpp',
  link_with: libdep,
  cpp_module_interfaces: ['api.cppm'],
  cpp_internal_partitions: ['hidden-impl.cppm'],
  cpp_private_module_interfaces: private_ifaces)

# The executable only ever imports api -- mylib's public interface -- and
# never touches detail or hidden directly.
prog = executable('prog', 'main.cpp', link_with: mylib)
test('prog', prog)
