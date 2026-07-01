project('submod', 'cpp', default_options: ['cpp_std=c++20'])

submodlib = static_library('submodlib', 'submod.cppm')
submod_dep = declare_dependency(link_with: submodlib)
