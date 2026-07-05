## C++20 named modules with GCC

Meson can now build C++20 named modules (and module partitions) with GCC on the
Ninja backend. Sources are scanned with GCC's P1689 dependency scanner and
ordered through Ninja `dyndep`; compiled module interfaces live in a single shared
`gcm.cache` at the build root, found by name like headers. No `-fmodule-file=`
mappings or module names appear on any compile command line.

Module interfaces produced at build time by a `generator()` or `custom_target`
are supported: the scan runs after the source is generated, and the module name
is discovered by that scan (only the output *filename* needs to be declared).

A target is treated as module-enabled when it contains a module-interface source
(`.cppm` / `.ixx`, including a generated one), when it links a target that
provides modules, or when the new `cpp_modules` build-target keyword is set:

```meson
modlib = static_library('modlib', 'modlib.cppm')
# The executable imports modlib's module and only links the library; no module
# names or sources of modlib are repeated here.
executable('prog', 'main.cpp', link_with: modlib)
```

`import std;` and `import std.compat;` are available through `dependency('std')`.
Meson locates the standard library's module sources from the selected libstdc++
(GCC >= 15, which ships `libstdc++.modules.json`) and builds them into the shared
cache as an ordinary module-providing library; linking that dependency both
resolves the imports and puts the standard library's module objects on the link
line. A target that only links another target which imports std -- without
importing std itself -- gets those objects transitively and needs no declaration
of its own. Like other reserved dependency names, an explicit
`meson.override_dependency('std', ...)` takes precedence over this synthesis if a
project needs to supply the standard-library modules itself.

`import std;` works in any modules-capable dialect (`cpp_std=c++20` or later with
GCC); it is not restricted to `c++23`. As always, `cpp_std` still governs which
standard-library *facilities* are available (for example `std::println` needs
`c++23`), exactly as it does for `#include`-based code.

```meson
std = dependency('std')
# main.cpp contains `import std;`. Declaring the dependency also marks the target
# module-enabled, so no cpp_modules keyword is needed.
executable('prog', 'main.cpp', dependencies: std,
           override_options: ['cpp_std=c++20'])
```

This first cut is GCC-only (named modules GCC >= 14; `import std;` GCC >= 15).
Header units are not covered. All translation units in a build that shares
modules must use the same module-affecting flags (e.g. `cpp_std`).
