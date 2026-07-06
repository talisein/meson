project('msvc undeclared header unit', 'cpp', default_options: ['cpp_std=c++latest'])

# prog declares one header unit but its source imports a second, undeclared one.
# The scan reports both (cl reports a header-unit require cold, with a
# lookup-method); the collator must fail with a clear error naming the missing
# declaration rather than letting the compile fail later with C7612.
executable('prog', 'main.cpp', cpp_header_units: ['declared.h'])
