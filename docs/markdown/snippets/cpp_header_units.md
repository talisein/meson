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
