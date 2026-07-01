## C++20 named modules with GCC

Meson can now build C++20 named modules (and module partitions) with GCC on the
Ninja backend. Sources are scanned with GCC's P1689 dependency scanner and
ordered through Ninja `dyndep`; compiled module interfaces live in a single shared
`gcm.cache` at the build root, found by name like headers. No `-fmodule-file=`
mappings or module names appear on any compile command line.

A target is treated as module-enabled when it contains a module-interface source
(`.cppm` / `.ixx`), when it links a target that provides modules, or when the new
`cpp_modules` build-target keyword is set:

```meson
modlib = static_library('modlib', 'modlib.cppm')
# The executable imports modlib's module and only links the library; no module
# names or sources of modlib are repeated here.
executable('prog', 'main.cpp', link_with: modlib)
```

This first cut is GCC-only (GCC >= 14). Header units and `import std;` via this
path are not yet covered. All translation units in a build that shares modules
must use the same module-affecting flags (e.g. `cpp_std`).
