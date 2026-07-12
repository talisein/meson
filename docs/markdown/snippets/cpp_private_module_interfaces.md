## Mark C++ module interfaces private with `cpp_private_module_interfaces`

A library can now declare that some of its C++ module interfaces are
*private*: importable only by its own sources, never published to a target
that links it, and exempt from the build-tree-wide rule that a module name
have only one providing target:

```meson
library('mylib', 'api.cppm', 'detail.cppm', 'impl.cpp',
  cpp_module_interfaces: ['api.cppm'],
  cpp_private_module_interfaces: ['detail.cppm'])
```

Here, `detail` is importable from `mylib`'s own sources but not from anything
that links `mylib`, and two libraries may each declare their own private
`detail` module without one having to know the other exists, as long as
they are never linked into the same program: a module's exported entities
are mangled from its bare module name alone, so two same-named modules —
private or not — reaching the same link is still a hard error, exactly as
it is for two public ones.

Like `cpp_module_interfaces`, the keyword takes sources, not module names: a
module's name is only known after a build-time scan, but the directory a
private interface's BMI belongs in must be decided at setup time. A source
may not be listed in both `cpp_module_interfaces` and
`cpp_private_module_interfaces` — being private already implies being an
interface. A partition does not automatically inherit privacy from its
primary module; list the partition's own source here too if it should be
private as well. Meson enforces this: leaving a private primary's partition
undeclared is a build-time error, not a silently-accepted gap.

Declaring this on an `executable()` is accepted but redundant: nothing can
ever link an executable, so all of its modules are already private.

An import of a private module from outside its defining target is rejected
with a diagnostic naming both the module and the target that provides it
privately, rather than the generic "provided by no target" error.
