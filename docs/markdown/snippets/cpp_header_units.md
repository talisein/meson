## Opt-in C++ header units with GCC, MSVC and Clang

A build target can declare the C++ header units its sources import, alongside
named modules, with GCC, MSVC or Clang on the Ninja backend. Each entry is the header
*as spelled in the import*, resolved on the target's include path — quoted for a
user header, angle-wrapped for a system header:

```meson
static_library('mylib', 'wrap.cppm',
  # wrap.cppm does `export import "dpp/dpp.h";` and `export import <cpr/cpr.h>;`
  cpp_header_units: ['dpp/dpp.h', '<cpr/cpr.h>'],
  dependencies: [dpp_dep, cpr_dep])
```

A header-unit BMI must exist before the sources that import it are scanned and
compiled, so the declaration is the key: Meson pre-builds each unit's BMI from
the declared list. On GCC consumers find the BMI by asking a module mapper
program Meson provides, so no BMI path reaches a command line; MSVC has no such
lookup for header
units, so Meson passes each consumer an explicit
`/headerUnit:<spelling>=<bmi>` mapping (the only place a header-unit BMI path
appears — never a named-module one). MSVC needs `cpp_std=c++20` or later.

Clang sits on the MSVC side of that split: it has no ambient header-unit
lookup at all (`-fprebuilt-module-path` does not apply to header units), so
Meson passes each consumer — and its dependency scan — an explicit
`-fmodule-file=<bmi>` per declared unit. Clang also considers header units an
experimental feature and warns (`-Wexperimental-header-units`) on every
import; Meson does not suppress the warning, since declaring
`cpp_header_units` is exactly the informed opt-in it exists to prompt. Add
`-Wno-experimental-header-units` to `cpp_args` to accept the feature's status
and silence it.

Current limitations:

- **Ordering.** Header units are built before all of the target's scans and
  compiles, but *not relative to each other*, so a header unit that itself
  `import`s another header unit is not supported. Units that only `#include`
  their prerequisites (absorbed textually) are fine.
- Every header unit a source imports must be declared on its target; the
  declaration does not propagate through dependencies.
- **Generated headers.** With GCC and Clang a declared header may be one the
  target writes during the build (a `custom_target` or `generator` output on
  its include path); its unit is built after the header is generated.
- **Spellings.** Prefer one include-path spelling from every importer: that is
  the only form portable to every compiler.
  - *GCC* finds a unit by the real path of whatever spelling an import
    resolves through, so `import "../hdr.h";` from a subdirectory reaches the
    unit declared as `'hdr.h'`, and declaring a second spelling of one file
    costs nothing: one file, one BMI per flag class. A header declared nowhere
    is an error at the importer's dependency scan, naming the target to add it
    to.
  - *Clang* matches a declared unit from any spelling of the same file.
  - *MSVC* matches a unit by the *literal* import spelling it scans, so
    `import "../hdr.h";` matches no target-level declaration: a second
    declared spelling (`sub/../hdr.h`) still mismatches the scanned
    `../hdr.h`, and the bare spelling cannot be resolved to a file from the
    target's directory. Meson reports it up front as a header unit "not
    declared in this target's cpp_header_units". Use the include-path
    spelling.
- **GCC before 16 and spaced install paths.** GCC 14 and 15 split the mapper
  program's command line at spaces and cannot quote it. Where the command
  running Meson holds a space, quote or backslash (a Python or Meson installed
  under such a path), Meson starts the mapper as a bare `meson` if that same
  Meson is on `PATH`, which the build must then also see; otherwise setup
  fails for that compiler's module targets. Use GCC 16, install Meson and
  Python at paths without spaces, or put Meson on `PATH`. Source and build
  paths with spaces are fine on every GCC.
- **ccache** does not track BMI contents and would serve stale objects for
  Clang module and header-unit consumers, so Meson makes it fall back to the
  real compiler for module compiles (as it already does for GCC's
  `-fmodules`); module-enabled sources do not benefit from ccache.
- A unit's BMI freezes the module-affecting flags it was built with, so targets
  that declare the same header unit with divergent module-affecting flags get a
  BMI of it each, one per flag equivalence class, on every supported compiler.
  Sharing a unit across targets with different `cpp_args` is therefore fine.
