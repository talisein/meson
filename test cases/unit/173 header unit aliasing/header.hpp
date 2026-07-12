#pragma once

inline constexpr int HV = 7;

// Observes a BMI-affecting define, and is only ever constant-evaluated, so each
// importer bakes in the value from the BMI it compiled against. constexpr rather
// than inline: an inline body emits a weak symbol the linker collapses across the
// two classes, and at -O0 every caller would reach the one survivor.
constexpr int hv_foo() {
#ifdef FOO
    return 1;
#else
    return 0;
#endif
}
