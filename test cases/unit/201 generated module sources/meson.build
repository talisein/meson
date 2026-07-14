project('generated module sources', 'cpp', default_options: ['cpp_std=c++20'])

# The C++ module surface reachable when the sources are written during the
# build (custom_target / generator outputs) rather than checked in. A module
# unit is recognised the same way a static one is -- by the cppm/ixx extension
# for an interface, or by the build-time scan for an implementation unit or a
# consumer. The three module kwargs also accept generated sources: an entry may
# be the custom_target itself, one of its indices, or a plain string naming one
# of its outputs, resolved by output name at configure time (only the filename
# is read, never the contents). That is what lets a generated internal
# partition (a non-interface extension MSVC must flag /internalPartition) and a
# generated private interface in a library be declared at all -- neither has any
# other channel.

py = find_program('python3')

gen = {}
foreach kind, name : {
    'primary': 'pkg.cppm',
    'part': 'pkg-part.cppm',
    'detail': 'pkg-detail.cpp',
    'detail2': 'pkg-detail2.cpp',
    'implunit': 'pkg-impl.cpp',
    'consumer': 'consumer.cpp',
    'wrapiface': 'wrap.cppm',
  }
  gen += {kind: custom_target(name,
    output: name,
    command: [py, files('gen.py'), kind, '@OUTPUT@'])}
endforeach

# A generated primary interface, a generated interface partition it re-exports,
# two generated internal partitions it imports, and a generated implementation
# unit, all in one library; imported across the link by a generated consumer
# that carries nothing but an `import pkg;`. The internal partitions are .cpp
# (no interface extension), so they are declared through cpp_internal_partitions
# -- one by object form (the custom_target itself), one by string form (naming
# the generated output) -- their only channel on MSVC.
pkglib = static_library('pkg',
  gen['primary'], gen['part'], gen['detail'], gen['detail2'], gen['implunit'],
  cpp_internal_partitions: [gen['detail'], 'pkg-detail2.cpp'])
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

# A generated private interface in a library. Each library provides a private
# module 'stash' from a generated .cppm declared through
# cpp_private_module_interfaces -- by string form for the first, by object form
# for the second -- behind a public interface 'faceN' whose implementation unit
# (not its interface) imports stash, so no importer's interface reaches the
# private module. An executable imports faceN across the link. Both libraries
# reuse the private module name 'stash' with different values: privacy keeps
# them apart in the shared BMI cache, exactly like the private-by-construction
# executables above -- a library's only route to that guarantee is the kwarg.
foreach conf : [['face1', '42', '43', 'string'], ['face2', '7', '8', 'object']]
  fmod = conf[0]
  stash = custom_target(fmod + '-stash',
    output: fmod + '-stash.cppm',
    command: [py, files('gen.py'), 'privmod', '@OUTPUT@', conf[1]])
  facepub = custom_target(fmod + '-pub',
    output: fmod + '.cppm',
    command: [py, files('gen.py'), 'facepub', '@OUTPUT@', fmod])
  faceimpl = custom_target(fmod + '-impl',
    output: fmod + '-impl.cpp',
    command: [py, files('gen.py'), 'faceimpl', '@OUTPUT@', fmod])
  facemain = custom_target(fmod + '-main',
    output: fmod + '-main.cpp',
    command: [py, files('gen.py'), 'facemain', '@OUTPUT@', fmod, conf[2]])
  # String form names the generated output; object form passes the custom_target.
  private_decl = conf[3] == 'string' ? fmod + '-stash.cppm' : stash
  facelib = static_library(fmod + 'lib', stash, facepub, faceimpl,
    cpp_private_module_interfaces: [private_decl])
  faceprog = executable(fmod + 'prog', facemain, link_with: facelib)
  test(fmod + 'prog', faceprog)
endforeach
