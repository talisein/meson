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
compiled, so the declaration is the key: Meson pre-builds each unit's BMI once
(shared across the build) from the declared list. On GCC consumers resolve the
BMI by directory lookup with no BMI path on any command line; MSVC has no such
lookup for header units, so Meson passes each consumer an explicit
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
- **Spelling discipline.** An importer-relative spelling that traverses
  directories (`import "../hdr.h";` from a subdirectory source) is fragile and
  handled differently by each compiler, so **prefer one include-path spelling
  from every importer** — that shares a single BMI regardless of the importing
  file's directory, and is the only portable form.
  - *GCC* keys a unit's BMI by the *textual* resolved name without normalizing
    it, so `import "../hdr.h";` from a subdirectory resolves to a name of its
    own and misses the declared unit's BMI (the scan fails with GCC's "imports
    must be built before being imported"). If an aliased spelling is
    unavoidable, declare it as its own unit spelled the way the importer
    resolves it (`cpp_header_units: ['hdr.h', 'sub/../hdr.h']`), which builds a
    second BMI of the same header.
  - *Clang* normalizes the resolved file and matches the declared unit from any
    spelling.
  - *MSVC* matches a unit by the *literal* import spelling it scans, so
    `import "../hdr.h";` matches no target-level declaration and there is **no
    alias escape hatch**: the GCC-style resolved-path spelling still mismatches
    the scanned `../hdr.h`, and the bare spelling cannot be resolved to a file
    from the target's directory. Meson reports it up front as a header unit
    "not declared in this target's cpp_header_units". Use the include-path
    spelling.
- **GCC: a header unit under a path with spaces needs a space-free link.** GCC
  resolves modules through a per-TU module mapper, which names each header unit
  by its resolved header path. A mapper key ends at the first space or tab and
  has no quoting or escape form, so a header whose path contains whitespace
  cannot be named directly; Meson routes it through a space-free link in the
  build directory instead, and the unit builds normally. The link is a symlink
  on POSIX, and on Windows a directory symlink where the build has that
  privilege (an elevated shell, or Developer Mode enabled) or otherwise an NTFS
  junction, which any user can create on a local NTFS volume. Only where none of
  these can be made — a non-NTFS build volume such as FAT/exFAT, or a build
  without symlink privilege on a filesystem that also cannot hold a junction —
  does the unit stay unnameable; the import then fails to compile with "no such
  module", and Meson warns about it at configure time. In that case move the
  source tree to a path without spaces, or enable Developer Mode. Named modules
  are unaffected everywhere, since a module name cannot contain whitespace.
- **ccache** does not track BMI contents and would serve stale objects for
  Clang module and header-unit consumers, so Meson makes it fall back to the
  real compiler for module compiles (as it already does for GCC's
  `-fmodules`); module-enabled sources do not benefit from ccache.
- A unit's BMI freezes the module-affecting flags it was built with. As with
  Meson's module support in general, header units expect uniform
  module-affecting flags across the build: every target that declares a given
  header unit shares one BMI (built from the first declaring target's flags),
  so diverging the flags for a header unit between targets is undefined
  behavior. This matches GCC's module cache, which is keyed by header path
  rather than flags and so can only hold one BMI per header regardless.
