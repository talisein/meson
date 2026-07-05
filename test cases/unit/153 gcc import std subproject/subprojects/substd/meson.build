project('substd', 'cpp', default_options: ['cpp_std=c++23'])

# The subproject independently asks for the standard library as modules.
std = dependency('std')

# A module-providing static library that imports std, exported to the parent.
submod = static_library('submod', 'submod.cppm', dependencies: std)
