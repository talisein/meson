project('gcc fmodules-ts legacy', 'cpp')

# A legacy target that passes -fmodules-ts in cpp_args but uses no C++ modules
# (no module-interface source, no cpp_modules kwarg, links no module provider).
# It must NOT be routed to the GCC P1689 module pipeline (which requires
# GCC >= 14); it keeps the existing scan path, as it did before named-module
# support existed.
executable('prog', 'main.cpp', cpp_args: ['-fmodules-ts'])
