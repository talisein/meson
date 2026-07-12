project('export dynamic private modules', 'cpp', default_options: ['cpp_std=c++20'])

mode = get_option('mode')

# An export_dynamic executable can be linked, so -- unlike an ordinary
# executable, whose modules are all private by construction because nothing
# could ever import them -- it is a normal module provider: pub is published
# to the shared BMI cache for whoever links it, and only the declared priv is
# private, exactly as for a library.
app = executable('app', 'pub.cppm', 'priv.cppm', 'main.cpp',
  export_dynamic: true,
  cpp_module_interfaces: ['pub.cppm'],
  cpp_private_module_interfaces: ['priv.cppm'])
test('app', app)

# The plugin the executable would dlopen/LoadLibrary: it links the executable
# and imports its public module. Under -Dmode=import-private it imports priv
# instead, which must be rejected -- a private module is importable only
# inside the target that provides it, linkable target or not.
plug_src = mode == 'import-private' ? 'plug_private.cpp' : 'plug.cpp'

if mode == 'divergent'
  # A diverging dialect puts the plugin in another BMI class, forcing a
  # BMI-only variant of pub whose provider is the executable itself. The
  # variant must recompile pub alone: priv is private and never crosses a
  # class boundary.
  shared_module('plug', plug_src, link_with: app,
                override_options: ['cpp_std=' + get_option('divergent_std')])
else
  shared_module('plug', plug_src, link_with: app)
endif
