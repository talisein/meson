## C++20 named modules with MSVC

C++20 named modules (and module partitions) now build with MSVC on the Ninja
backend, through the same pipeline used for GCC. Sources are scanned with cl's
`/scanDependencies` P1689 output and ordered through Ninja `dyndep`;
compiled module interfaces (`.ifc`) live in a single shared `ifc.cache` at the
build root and are found by name through `/ifcSearchDir`. No `/reference`
mappings or module names appear on any compile command line.

The user-facing interface is identical to the GCC one: a target is
module-enabled when it has a module-interface source (`.cppm` / `.ixx`, including
a build-time generated one), when it links a target that provides modules, or
when the `cpp_modules` keyword is set.

```meson
modlib = static_library('modlib', 'modlib.ixx')
# The executable imports modlib's module and only links the library.
executable('prog', 'main.cpp', link_with: modlib)
```

As with GCC, module usage is declared, never sniffed — Meson never reads
source contents to detect modules. cl requires each interface unit to be
compiled with `/interface`, and Meson derives that per-source flag from the
`.cppm`/`.ixx` extension — or from the target's `cpp_module_interfaces` list,
which marks a source an interface unit regardless of its extension, so an
`export module` in a plain `.cpp` now builds. `cpp_modules: true` on its own
still only covers consumers whose sources merely `import`.

This replaces the previous MSVC behavior, where every target built with
`cpp_std=c++latest` was scanned for modules by reading its sources. A project
that relied on that content-based detection must now declare: renaming the
module interfaces to `.ixx` (or `.cppm`) is sufficient, and consumers that
link a module-providing target need nothing at all.

`import std;` and `import std.compat;` are available through `dependency('std')`,
built from the standard library's module sources shipped with the MSVC toolset
(`modules.json`). The P1689 scan requires Visual Studio 2022 17.2 or newer
(cl 19.32, where `/scanDependencies` shipped) and `cpp_std=c++20` or later.
As on GCC and Clang, `dependency('std')` folds in the threads dependency by
default (a no-op on MSVC, where threading needs no flags), and
`dependency('std-nothreads')` opts out; the two cannot be mixed in one build.

Older but modules-capable MSVC (VS 2019 16.8 up to VS 2022 17.1) falls back to
the previous regex-based scanner, which handles only flat named modules --
module partitions and `import std;` need the P1689 scan and therefore VS 2022
17.2 or newer.

Diagnostics (a module required by no target, a duplicate module name reaching one
link, and module dependency cycles) are reported at build time for MSVC exactly
as for GCC. All translation units sharing modules must use the same
module-affecting flags.
