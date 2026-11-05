---
short-description: Auto-detection of features like ccache and code coverage
...

# Feature autodetection

Meson is designed for high productivity. It tries to do as many things
automatically as it possibly can.

Ccache
--

[Ccache](https://ccache.dev/) is a cache system designed to make
compiling faster. When you run Meson for the first time for a given
project, it checks if Ccache is installed. If it is, Meson will use it
automatically.

If you do not wish to use Ccache for some reason, just specify your
compiler with environment variables `CC` and/or `CXX` when first
running Meson (remember that once specified the compiler cannot be
changed). Meson will then use the specified compiler without Ccache.

### C++ modules

A compiler cache cannot cache a C++ module compile correctly. It does
not hash the compiled module interfaces (BMIs) a translation unit
imports, so after an imported module changes it can hand back an object
built against the old one; and it does not know a BMI is an output, so a
cache hit can leave the BMI missing. Meson therefore never runs a cache
it added itself on a module compile, scan or BMI build; the target's
other compiles still use it.

Meson cannot see a cache that is not on its own command line, such as
a distribution that puts Ccache in front of the compiler through `PATH`
(Fedora's `/usr/lib64/ccache`, for example: there `c++` and `clang++`
are Ccache). Turn Ccache off for module projects there, by building with
`CCACHE_DISABLE=1` in the environment, or by configuring with the
compiler's real path (`CXX=/usr/bin/clang++`). A bare `CXX=clang++`
still finds the masquerading one.

[sccache](https://github.com/mozilla/sccache) is auto-detected the
same way, and is preferred over Ccache when both are installed.

[BuildCache](https://gitlab.com/bits-n-bites/buildcache) is
auto-detected as well, after Ccache and sccache.

[kache](https://github.com/kunobi-ninja/kache) is also supported, but
is never added automatically: it must be invoked with a compiler
subcommand (`kache cc`, `kache c++`, ...), so Meson cannot safely
prepend it to an arbitrary compiler. To use it, put it in front of
the compiler yourself, e.g. `CC="kache cc"`.

Coverage
--

When doing a code coverage build, Meson will check for existence of
the binaries `gcovr`, `lcov` and `genhtml`. If version 3.3 or higher
of the first is found, targets called *coverage-text*, *coverage-xml*
and *coverage-html* are generated. If version 4.2 or higher of the
first is found, targets *coverage-text*, *coverage-xml*, *coverage-sonarqube*
and *coverage-html* are generated. Alternatively, if the latter two
are found, only the target *coverage-html* is generated. Coverage
reports can then be produced simply by calling e.g. `meson compile
coverage-xml`. As a convenience, a high-level *coverage* target is
also generated which will produce all 3 coverage report types, if
possible.

Note that generating any of the coverage reports described above
requires the tests (i.e. `meson test`) to finish running so the
information about the functions that are called in the tests can be
gathered for the report.
