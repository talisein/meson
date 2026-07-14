// constexpr, and only ever constant-evaluated by importers: the value is
// baked into each TU from the BMI it compiled against, and no weak symbol is
// emitted for the linker to collapse across the two divergent classes.
constexpr int util_cpp() {
    return __cplusplus > 202002L ? 23 : 20;
}
