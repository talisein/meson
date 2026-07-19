project('clang import std stdlib link conflict', 'cpp', default_options: ['cpp_std=c++23'])

# The parent uses the platform-default standard library (libstdc++ here).
std = dependency('std')

# A subproject builds a module-providing library against libc++, which imports
# std and thus synthesizes its own (libc++) std library.
sub = subproject('cxxlib')
cxxmod = sub.get_variable('cxxmod')

# prog links BOTH standard libraries into one executable: the parent's
# libstdc++ std (it imports std directly) and the subproject's libc++ std
# (reached transitively through cxxmod). An impossible mix of standard
# libraries: the foreign libc++ std cannot be recompiled into prog's BMI
# class (it lacks -stdlib=libc++, so the libc++ interfaces cannot find their
# support headers), and the build fails hard rather than linking mismatched
# standard libraries.
prog = executable('prog', 'main.cpp', dependencies: std, link_with: cxxmod)
test('prog', prog)
