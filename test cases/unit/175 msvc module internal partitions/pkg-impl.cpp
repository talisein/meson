// An internal partition: no export anywhere, yet it provides pkg:impl with an
// importable BMI (the scanner reports it with is-interface: false). On MSVC an
// internal partition uses a non-interface extension (.cpp) and /internalPartition.
module pkg:impl;
import :part;

#if defined(__GXX_RTTI) || defined(_CPPRTTI)
constexpr bool impl_rtti = true;
#else
constexpr bool impl_rtti = false;
#endif

int hidden() {
    return 41 + (part_val() - part_val());
}
