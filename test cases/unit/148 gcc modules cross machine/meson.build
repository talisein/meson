project('gcc modules cross machine', 'cpp',
        default_options: ['cpp_std=c++20', 'build.cpp_std=c++20'])

# One build, two machines, disjoint module caches. Under cross-compilation the
# build-machine and host-machine compilers write incompatible BMIs, so the two
# used to be barred from a single shared cache at the build root. They are now
# kept apart by a per-machine class subdir, and the same module NAME 'foo' on
# both machines -- exactly the case that used to collide -- coexists.

# The host machine's module library and its consumer.
host_mod = static_library('hostmod', 'mod.cppm')
host_prog = executable('hostprog', 'main.cpp', link_with: host_mod)
# Identity cross: the host binary runs on the build machine, so it is tested
# like any native one.
test('hostprog', host_prog)

# The build machine's module library and consumer, same module name 'foo'.
build_mod = static_library('buildmod', 'mod.cppm', native: true)
build_prog = executable('buildprog', 'main.cpp', native: true, link_with: build_mod)
test('buildprog', build_prog)

# A header unit declared on each machine. Per-machine alias roots must name the
# two machines' unit BMIs apart, the same way divergent flag classes do.
host_hu = executable('hosthu', 'usehu.cpp', cpp_header_units: ['head.hpp'])
test('hosthu', host_hu)
build_hu = executable('buildhu', 'usehu.cpp', native: true, cpp_header_units: ['head.hpp'])
test('buildhu', build_hu)

# `import std` on both machines (opt-in: needs a compiler shipping the std
# module on each machine, and c++23). Each machine synthesizes its own std
# library, with a distinct target name, and both build and run.
if get_option('std')
  host_std = dependency('std')
  host_stdprog = executable('hoststdprog', 'stdmain.cpp', dependencies: host_std)
  test('hoststdprog', host_stdprog)

  build_std = dependency('std', native: true)
  build_stdprog = executable('buildstdprog', 'stdmain.cpp', native: true,
                             dependencies: build_std)
  test('buildstdprog', build_stdprog)
endif
