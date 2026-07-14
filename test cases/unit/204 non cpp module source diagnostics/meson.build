project('non cpp module source diagnostics', 'c', 'cpp',
  default_options: ['cpp_std=c++20'])

# A module kwarg names an interface/partition unit among the target's sources.
# A source that the target does not compile as C++ -- a C file in a
# mixed-language target, or a generated header -- can declare no module unit:
# it is compiled by another language's rules (or not at all) and never
# scanned, so the declaration would silently do nothing. Each mode names such a
# source through a different entry form; all fail during setup, before anything
# is built. The generated forms already had this check; the static string and
# files() forms did not, so the same mistake was caught for a generated source
# but silently ignored for a checked-in one.

py = find_program('python3')
mode = get_option('mode')

if mode == 'static-string'
  # A C source of a mixed-language target, named by string. It is one of the
  # target's sources, so the typo check passes -- but it is not a C++ source.
  static_library('lib', 'anchor.c', 'iface.cppm',
    cpp_module_interfaces: ['anchor.c'])
elif mode == 'static-file'
  # The same C source, passed as a files() object rather than a string.
  static_library('lib', 'anchor.c', 'iface.cppm',
    cpp_module_interfaces: [files('anchor.c')])
elif mode == 'generated-string'
  # A custom_target emitting a real interface alongside a header. The header is
  # a legitimate carried-along output (harmless when the custom_target is passed
  # whole), but naming it directly asks for that specific, non-C++ output.
  gen = custom_target('gen',
    output: ['gen_iface.cppm', 'gen_config.h'],
    command: [py, files('gen.py'), '@OUTDIR@'])
  static_library('lib', gen,
    cpp_private_module_interfaces: ['gen_config.h'])
endif
