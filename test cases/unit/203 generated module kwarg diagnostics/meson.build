project('generated module kwarg diagnostics', 'cpp', default_options: ['cpp_std=c++20'])

# Configure-time diagnostics for naming generated sources in the C++ module
# kwargs. Every mode fails during setup, before anything is compiled.

py = find_program('python3')
mode = get_option('mode')

gen_dup = custom_target('gen-dup',
  output: 'dup.cppm',
  command: [py, files('gen.py'), '@OUTPUT@'])

if mode == 'ambiguity'
  # A string that matches both a static source and a generated output of the
  # same name: the resolver cannot tell which the user meant, so it must ask
  # for the files() object or the custom_target rather than guess.
  static_library('lib', 'dup.cppm', gen_dup,
    cpp_module_interfaces: ['dup.cppm'])
elif mode == 'both-kwargs'
  # The same generated output reached two ways -- named by string in
  # cpp_module_interfaces and passed as its custom_target in
  # cpp_internal_partitions -- must still trip the one-or-the-other error, the
  # cross-kwarg check working on canonical keys, not on entry form.
  static_library('lib', gen_dup,
    cpp_module_interfaces: ['dup.cppm'],
    cpp_internal_partitions: [gen_dup])
elif mode == 'typo'
  # A string that matches neither a static source nor any generated output:
  # the typo error still fires, now noting generated outputs were searched too.
  static_library('lib', gen_dup,
    cpp_module_interfaces: ['nosuchmodule.cppm'])
endif
