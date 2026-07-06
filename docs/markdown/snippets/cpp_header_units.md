## Experimental: opt-in C++ header units with GCC and MSVC

A build target can declare the C++ header units its sources import, alongside
named modules, with GCC or MSVC on the Ninja backend. Each entry is the header
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

This is an early, deliberately minimal prototype. Known limits:

- **Ordering.** Header units are built before all of the target's scans and
  compiles, but *not relative to each other*, so a header unit that itself
  `import`s another header unit is not supported. Units that only `#include`
  their prerequisites (absorbed textually) are fine.
- Every header unit a source imports must be declared on its target; the
  declaration does not propagate through dependencies.
- A unit's BMI freezes the module-affecting flags it was built with. As with
  Meson's module support in general, header units expect uniform
  module-affecting flags across the build: every target that declares a given
  header unit shares one BMI (built from the first declaring target's flags),
  so diverging the flags for a header unit between targets is undefined
  behavior. This matches GCC's module cache, which is keyed by header path
  rather than flags and so can only hold one BMI per header regardless.
