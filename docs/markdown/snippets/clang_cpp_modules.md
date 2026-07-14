## C++20 named modules with Clang

C++20 named modules (and module partitions, internal implementation
partitions included) now build with Clang on the Ninja backend,
through the same pipeline used for GCC and MSVC. Sources are scanned
with `clang-scan-deps` (P1689) and ordered through Ninja `dyndep`; compiled
module interfaces (`.pcm`) live in a single shared `pcm.cache` at the build
root and are found by name through `-fprebuilt-module-path`. No
`-fmodule-file=` mappings or module names appear on any compile command line.

The scanner requirement is detected by running `clang-scan-deps`, not by
version: Meson looks for the tool next to the compiler (then on `PATH`) and
verifies its P1689 support with a probe, so any Clang whose scanner works is
supported. If a module-using target is configured with Clang and no working
scanner is found, setup fails with a clear message.

The user-facing interface is identical to the GCC and MSVC ones: a target is
module-enabled when it has a module-interface source (`.cppm` / `.ixx`,
including a build-time generated one), when it links a target that provides
modules, or when the `cpp_modules` keyword is set. Clang decides what is an
interface unit per source (Meson passes `-x c++-module`), so a module interface
must be identified up front — by the `.cppm`/`.ixx` extension, or by listing it
in the target's `cpp_module_interfaces`, which marks a source an interface unit
regardless of its extension (so an `export module` in a plain `.cpp` now builds).

Because Clang names a compiled interface after its *source* file, Meson
publishes each interface's BMI into the shared cache under its scanned module
name as a small build-time step; this is invisible in the API and keeps every
compile command line static.

`import std;` and `import std.compat;` are available through
`dependency('std')`, exactly as on GCC and MSVC. The dependency is threaded
by default — the std module is built with the threads dependency's flags and
every consumer inherits them, so the POSIX-thread setting Clang bakes into
`std.pcm` matches all of its importers by construction;
`dependency('std-nothreads')` opts out (one shared std module per machine, so
the two spellings cannot be mixed on one machine). Clang serves two standard
libraries, so Meson probes which one the build actually uses — honoring the
platform default, Clang configuration files, and a `-stdlib=libc++` given via
`cpp_args`, `add_global_arguments` or `add_project_arguments` — and builds the
std module from that library's own manifest: libstdc++'s
`libstdc++.modules.json` (GCC >= 15 installed alongside) or libc++'s
`libc++.modules.json`. Per-target `-stdlib` divergence is not supported: a
compiled module bakes in its standard library.

```meson
std = dependency('std')
executable('prog', 'main.cpp', dependencies: std,
           override_options: ['cpp_std=c++23'])
```

Diagnostics (a module required by no target, a duplicate module name reaching
one link, and module dependency cycles) are reported at build time exactly as
for GCC and MSVC. A BMI bakes in the flags of its own compile, and Clang
refuses a mismatched import with a hard error (a `cpp_std` mismatch, or
`-pthread` present on one side only — `dependency('threads')` adds it only to
its own consumers). Targets that share modules but compile with divergent
module-affecting flags nonetheless build correctly: each flag class gets its
own subdirectory of `pcm.cache`, and Meson recompiles a shared provider's
interfaces per class as BMIs only — every consumer still links the provider's
objects exactly once. The std module needs no special handling for any of
this; `dependency('std')` folds the threads dependency in and propagates it
to every consumer. The same separation holds across a cross build's two
machines, exactly as for GCC: each machine keeps its own `pcm.cache` entries,
so a build-machine module target and a host-machine one never contend for a
BMI, even under the same module name.

ccache does not track BMI contents and would serve stale objects for module
consumers, so Meson makes it fall back to the real compiler for module
compiles (as it already does for GCC's `-fmodules`); module-enabled sources
do not benefit from ccache. A warning at setup points this out when ccache
wraps the compiler, whether as a launcher or through distro PATH masquerade.
