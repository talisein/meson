project('cxxlib', 'cpp', default_options: ['cpp_std=c++23'])

# This subproject builds against libc++, so its dependency('std') synthesizes a
# std library distinct from the parent's libstdc++ one.
add_project_arguments('-stdlib=libc++', language: 'cpp')
add_project_link_arguments('-stdlib=libc++', language: 'cpp')

std = dependency('std')
cxxmod = static_library('cxxmod', 'cxxmod.cppm', dependencies: std)
