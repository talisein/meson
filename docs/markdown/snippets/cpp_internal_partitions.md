## Declare C++ internal module partitions with `cpp_internal_partitions`

A build target can now name which of its C++ sources are *internal*
(implementation) module partitions — a `module pkg:impl;` unit with no `export`
— with the `cpp_internal_partitions` keyword (GCC, MSVC or Clang, on the Ninja
backend):

```meson
static_library('pkg', 'pkg.ixx', 'pkg-part.ixx', 'pkg-impl.cpp',
  cpp_internal_partitions: ['pkg-impl.cpp'])
```

An internal partition is still a BMI-producing module unit, so it is scanned,
ordered and (across a BMI-class split) recompiled like an interface. It differs
in one compiler detail: MSVC compiles it with `/internalPartition` rather than
`/interface`, and **cl rejects an interface file extension for it** — a `.ixx`
is a module-interface extension and is incompatible with `/internalPartition`.
Name the internal partition with a plain implementation extension (`.cpp`); the
same source compiles as an internal partition on GCC (which infers the unit
kind) and Clang (`-x c++-module`), so one `meson.build` is portable across all
three compilers.

The keyword is accepted on every supported compiler and only changes a flag on
MSVC; a GCC- or Clang-only project may leave the partition in its source without
declaring it, since those compilers infer the unit kind. Each entry must be one
of the target's own sources (a string or a `files()` object); a path that names
no source is an error, not a silent no-op.
