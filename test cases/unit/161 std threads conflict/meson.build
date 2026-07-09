project('std threads conflict', 'cpp', default_options: ['cpp_std=c++23'])

# One shared std module per build: mixing the threaded default with the
# nothreads opt-out must fail at setup.
dependency('std')
dependency('std-nothreads')
