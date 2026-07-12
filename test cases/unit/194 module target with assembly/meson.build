project('module target with assembly', 'cpp', default_options: ['cpp_std=c++20'])

# The C++ compiler also assembles: a .S in a module-enabled target is routed to
# it, but nothing scans it, so it provides and imports no module. Its compile
# edge must therefore carry none of the module pipeline (no per-TU module
# mapper, no scan) -- those per-source outputs exist only for scanned C++
# sources, and depending on one the collate never declares is a dangling edge
# ninja rejects up front.
modlib = static_library('modlib', 'modlib.cppm')
prog = executable('prog', 'main.cpp', 'asmpart.S', link_with: modlib)
test('prog', prog)
