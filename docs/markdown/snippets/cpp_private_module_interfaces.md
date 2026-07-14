## Mark C++ module interfaces private with `cpp_private_module_interfaces`

A library can now declare that some of its C++ module interfaces are
*private*: importable only by its own sources, never published to a target
that links it, and exempt from the rule that a module name have only one
providing target per module cache path -- itself already scoped to one
machine and flag class, so a public module can share a name with one built
for a different machine or flag class without being made private:

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

An entry may also name a generated source — a `custom_target`, one of its
indices, or a `generator().process()` output — which gives a generated
private interface a route to privacy it would otherwise lack entirely: a
library has no by-construction way to keep a build-time-produced module out
of what it publishes, so a dropped declaration would silently publish it.

Keep a *public* module's interface out of its private ones, though. A
translation unit that imports a module has an interface dependency on
everything that module's interface unit imports, and transitively on
everything those import in turn, so it may have to read the BMI of a
partition — or of any other module — the interface is built out of, and
privacy is exactly what keeps that BMI inside the target. Whether an importer
really needs it is unspecified: one compiler builds such a project happily,
another fails it at the importer, naming a module the project never wrote.
Meson therefore warns and lets the build proceed rather than refusing it,
and never publishes the BMI behind the declaration's back. The fix is usually
to import the private module from a module *implementation* unit
(`module pkg;`) rather than from `pkg`'s interface, which keeps it private
and needed nowhere else.

Declaring this on an ordinary `executable()` is accepted but redundant:
nothing can link one, so all of its modules are already private. On an
`export_dynamic` executable it is meaningful — a `shared_module` can link
such an executable, so its modules are published like a library's, and this
keyword names the ones that stay private.

An import of a private module from outside its defining target is rejected
with a diagnostic naming both the module and the target that provides it
privately, rather than the generic "provided by no target" error.
