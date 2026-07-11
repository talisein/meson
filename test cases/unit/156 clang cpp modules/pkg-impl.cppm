// An internal partition: provides pkg:impl with no export anywhere (the
// scanners report it with is-interface: false), yet its BMI must be built
// and ordered before the primary interface that imports it.
module pkg:impl;

int impl_value() {
    return 10;
}
