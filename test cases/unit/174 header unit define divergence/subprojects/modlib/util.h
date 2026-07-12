// constexpr, and only ever constant-evaluated by importers: the value is baked
// into each TU from the BMI it compiled against, and no weak symbol is emitted
// for the linker to collapse across the two divergent classes. An inline
// function would be deduped at link and, at -O0, every caller would reach the
// one surviving body -- hiding the miscompile this pins.
constexpr int util_foo() {
#ifdef FOO
    return 1;
#else
    return 0;
#endif
}
