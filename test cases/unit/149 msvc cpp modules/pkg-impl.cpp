// An internal partition: provides pkg:impl with no export anywhere (the
// scanner reports it with is-interface: false), yet its BMI must be built and
// ordered before the primary interface that imports it. On MSVC an internal
// partition must use a non-interface extension (.cpp) -- cl rejects an .ixx
// with /internalPartition -- and is declared via cpp_internal_partitions.
module pkg:impl;

int impl_value() {
    return 10;
}
