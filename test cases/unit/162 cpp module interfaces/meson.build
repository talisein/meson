project('cpp module interfaces', 'cpp', default_options: ['cpp_std=c++20'])

# A module interface unit that lives in a plain .cc file, declared as such with
# cpp_module_interfaces (string form). Without the declaration Clang and MSVC
# could not compile it as an interface unit (GCC infers it). impl.cc is a module
# implementation unit (module mymod;), not an interface, and is deliberately not
# listed. The executable merely links the library.
modlib = static_library('modlib', 'mod.cc', 'impl.cc',
  cpp_module_interfaces: ['mod.cc'])
prog = executable('prog', 'main.cpp', link_with: modlib)
test('prog', prog)

# cpp_module_interfaces also accepts files() objects, not just strings.
fmod = files('filemod.cc')
filelib = static_library('filelib', fmod, cpp_module_interfaces: fmod)
fileprog = executable('fileprog', 'filemain.cpp', link_with: filelib)
test('fileprog', fileprog)
