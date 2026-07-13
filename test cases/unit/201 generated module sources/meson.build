project('generated module sources', 'cpp', default_options: ['cpp_std=c++20'])

# The C++ module surface reachable when the sources are written during the
# build (custom_target / generator outputs) rather than checked in. A module
# unit is recognised the same way a static one is -- by the cppm/ixx extension
# for an interface, or by the build-time scan for an implementation unit or a
# consumer -- so nothing here names a generated file in a module kwarg. That is
# the boundary: cpp_internal_partitions and cpp_private_module_interfaces match
# against the target's static source list, so an internal partition or a
# kwarg-declared private interface cannot be a generated file. The private
# module below is instead private by construction (an unlinkable executable).

py = find_program('python3')

gen = {}
foreach kind, name : {
    'primary': 'pkg.cppm',
    'part': 'pkg-part.cppm',
    'implunit': 'pkg-impl.cpp',
    'consumer': 'consumer.cpp',
    'wrapiface': 'wrap.cppm',
  }
  gen += {kind: custom_target(name,
    output: name,
    command: [py, files('gen.py'), kind, '@OUTPUT@'])}
endforeach

# A generated primary interface, a generated interface partition it re-exports,
# and a generated implementation unit, all in one library; imported across the
# link by a generated consumer that carries nothing but an `import pkg;`.
pkglib = static_library('pkg', gen['primary'], gen['part'], gen['implunit'])
pkgprog = executable('pkgprog', gen['consumer'], link_with: pkglib)
test('pkgprog', pkgprog)

# A generated interface in the consumer role too: wrap imports pkg and
# re-exports on top of it, so it is scanned both as a provider and an importer.
wraplib = static_library('wrap', gen['wrapiface'], link_with: pkglib)
wrapprog = executable('wrapprog', 'wrapmain.cpp', link_with: [wraplib, pkglib])
test('wrapprog', wrapprog)

# Generated private modules. Nothing can link an executable, so the module it
# provides is private to it -- two executables generate the same module name
# with different values and must not collide in the shared BMI cache.
secret1 = custom_target('secret1.cppm',
  output: 'secret1.cppm',
  command: [py, files('gen.py'), 'secret', '@OUTPUT@', '99'])
secret2 = custom_target('secret2.cppm',
  output: 'secret2.cppm',
  command: [py, files('gen.py'), 'secret', '@OUTPUT@', '7'])
priv1 = executable('priv1', secret1, 'privmain1.cpp')
priv2 = executable('priv2', secret2, 'privmain2.cpp')
test('priv1', priv1)
test('priv2', priv2)
