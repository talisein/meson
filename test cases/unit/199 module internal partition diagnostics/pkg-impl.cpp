// An internal partition: no export anywhere, yet it provides pkg:impl with
// an importable BMI. A plain .cpp extension, since cl rejects an interface
// extension under /internalPartition.
module pkg:impl;

int hidden() {
    return 42;
}
