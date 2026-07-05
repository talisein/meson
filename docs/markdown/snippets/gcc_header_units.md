## Experimental: opt-in C++ header units with GCC

A build target can declare the C++ header units its sources import, alongside
named modules, with GCC on the Ninja backend. Each entry is the header *as
spelled in the import*, resolved on the target's include path — quoted for a
user header, angle-wrapped for a system header:

```meson
static_library('mylib', 'wrap.cppm',
  # wrap.cppm does `export import "dpp/dpp.h";` and `export import <cpr/cpr.h>;`
  cpp_header_units: ['dpp/dpp.h', '<cpr/cpr.h>'],
  dependencies: [dpp_dep, cpr_dep])
```

GCC's dependency scanner fails on a header-unit import whose BMI does not yet
exist, so the declaration is the key: Meson pre-builds each unit's BMI once
(shared) before scanning, and consumers resolve it by directory lookup with no
BMI path on any command line.

This is an early, deliberately minimal prototype (GCC only). Known limits:

- **Ordering.** Header units are built before all of the target's scans and
  compiles, but *not relative to each other*, so a header unit that itself
  `import`s another header unit is not supported. Units that only `#include`
  their prerequisites (absorbed textually) are fine.
- Every header unit a source imports must be declared on its target; the
  declaration does not propagate through dependencies.
- A unit's BMI freezes the preprocessor state it was built with, so the whole
  build must use uniform module-affecting flags.
