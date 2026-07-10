## Declare C++ module interface units by source with `cpp_module_interfaces`

A build target can now name which of its C++ sources are module *interface*
units, regardless of their file extension, with the `cpp_module_interfaces`
keyword (GCC, MSVC or Clang, on the Ninja backend):

```meson
static_library('mylib', 'mod.cc', 'impl.cc',
  cpp_module_interfaces: ['mod.cc'])
```

By default Meson identifies a module interface by its `.cppm`/`.ixx` extension,
never by reading the source. That extension is also how Clang and MSVC are told,
per source, to compile a translation unit as an interface unit (Clang:
`-x c++-module`; cl: `/interface`) — GCC infers it from the source itself. So a
project that uses a plain extension such as `.cc` for an `export module` builds
on GCC (with `cpp_modules: true`) but could not build at all on Clang or MSVC.

`cpp_module_interfaces` closes that gap: it is a per-source declaration — the
direct analogue of CMake's `FILE_SET CXX_MODULES` — that marks the listed
sources as interface units regardless of extension, so the same project is
portable across all three compilers. Existing `.cppm`/`.ixx` sources keep
working with no keyword.

Each entry must be one of the target's own sources (a string or a `files()`
object); a path that names no source is an error, not a silent no-op.
