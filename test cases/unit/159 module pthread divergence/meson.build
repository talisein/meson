project('clang module pthread divergence', 'cpp',
        default_options: ['cpp_std=c++20'])

# modlib builds without -pthread; dependency('threads') injects it into
# prog only, so the two module-sharing targets diverge on a flag Clang
# bakes into the BMI. Setup must warn.
modlib = static_library('modlib', 'modlib.cppm')
threads = dependency('threads')
executable('prog', 'main.cpp', link_with: modlib, dependencies: threads)
