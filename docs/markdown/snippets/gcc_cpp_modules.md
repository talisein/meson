## C++20 named modules with GCC

Meson can now build C++20 named modules (and module partitions,
internal implementation partitions included) with GCC on the Ninja
backend. Sources are scanned with GCC's P1689 dependency scanner and
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

Module usage is declared, never sniffed: Meson decides which targets get the
module machinery from file extensions, keywords and the link graph alone, and
never reads source contents. A module interface in a source with a non-module
extension (an `export module` in a plain `.cpp`/`.cc`) is therefore not
detected by itself — rename the interface to `.cppm`/`.ixx`, or set
`cpp_modules: true` on the target that carries it.

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

`dependency('std')` is threaded by default: the std module is built with the
threads dependency's flags and every consumer inherits them, so the
POSIX-thread setting compiled into the module matches all of its importers
by construction (Clang enforces this; see below). A build that must avoid
the thread flags can use `dependency('std-nothreads')` instead; there is a
single shared std module per build, so the two spellings cannot be mixed.

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

Common mistakes are reported at build time with a clear message instead of a
confusing link error: a module required by no target, a module name provided by
two sources reaching one link, and module dependency cycles.

GCC cannot combine precompiled headers with modules (a `-fmodules` compile
rejects any `.gch` as invalid), so `cpp_pch` on a module-enabled target is
disabled with a warning and the target builds as if `b_pch=false`. On Clang
and MSVC the two combine fine, except that a module interface unit itself
never gets the PCH: its forced include would land before the module
declaration, which is ill-formed.

This first cut is GCC-only (named modules GCC >= 14; `import std;` GCC >= 15).
Header units are not covered. Targets that share modules but compile with
divergent module-affecting flags (a different `cpp_std`, extra defines,
`-pthread` from `dependency('threads')`, ...) build correctly, as on Clang and
MSVC: each flag class gets its own subdirectory of `gcm.cache` — reached
through a per-translation-unit module mapper, since GCC has no module search
path flag — and Meson recompiles a shared provider's interfaces per class as
BMIs only, so every consumer still links the provider's objects exactly once.
A single-class build keeps the flat `gcm.cache` and mapper-less command
lines. Header units remain shared build-wide: a flag-divergent import of a
shared header unit still warns at setup.
