// An internal partition: no export anywhere, yet it provides pkg:impl with
// an importable BMI (the scanners report it with is-interface: false).
module pkg:impl;
import :part;

#ifdef FOO
constexpr bool impl_foo = true;
#else
constexpr bool impl_foo = false;
#endif

int hidden() {
    return 41 + (part_val() - part_val());
}
