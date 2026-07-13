project('private module reachability', 'cpp', default_options: ['cpp_std=c++20'])

# Every mode builds the same thing: a library providing the public module pkg,
# and a separate executable importing it. What differs is which unit of pkg
# imports the private one -- pkg's own interface (the shapes that warn) or a
# module implementation unit (the shape that must not).
mode = get_option('mode')

if mode == 'interface-import'
  # pkg's interface imports the internal partition :impl, which is private:
  # importers of pkg have an interface dependency on a BMI that stays in mylib.
  mylib = static_library('mylib', 'ii_pkg.cppm', 'ii_pkg-impl.cpp',
    cpp_internal_partitions: ['ii_pkg-impl.cpp'],
    cpp_private_module_interfaces: ['ii_pkg-impl.cpp'])
elif mode == 'interface-partition'
  # Same, but :part is an *interface* partition, which the primary must export
  # -- so importers of pkg reach it necessarily, not merely maybe.
  mylib = static_library('mylib', 'ip_pkg.cppm', 'ip_pkg-part.cppm',
    cpp_private_module_interfaces: ['ip_pkg-part.cppm'])
elif mode == 'indirect'
  # pkg's interface imports the public partition :part, which imports the
  # private :impl: the reach is two edges long, not one.
  mylib = static_library('mylib', 'in_pkg.cppm', 'in_pkg-part.cppm', 'in_pkg-impl.cpp',
    cpp_internal_partitions: ['in_pkg-impl.cpp'],
    cpp_private_module_interfaces: ['in_pkg-impl.cpp'])
elif mode == 'private-module'
  # Not a partition at all: pkg's interface imports the target's own private
  # module detail. Same interface dependency, same withheld BMI.
  mylib = static_library('mylib', 'pm_pkg.cppm', 'pm_detail.cppm',
    cpp_private_module_interfaces: ['pm_detail.cppm'])
else
  # The sound shape, and the one this must not warn about: only pkg's module
  # implementation unit imports the private :impl. Nothing outside mylib has an
  # interface dependency on it, so its BMI is needed nowhere else and privacy
  # costs nothing.
  mylib = static_library('mylib', 'iu_pkg.cppm', 'iu_pkg-impl.cpp', 'iu_pkg_unit.cpp',
    cpp_internal_partitions: ['iu_pkg-impl.cpp'],
    cpp_private_module_interfaces: ['iu_pkg-impl.cpp'])
endif

prog = executable('prog', 'main.cpp', link_with: mylib)
test('prog', prog)
