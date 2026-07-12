// An internal partition of a private primary module: privacy is not
// automatically inherited from hidden, so this source is independently
// listed in cpp_private_module_interfaces too (as well as
// cpp_internal_partitions, exactly as a public internal partition would be).
module hidden:impl;

int impl_value() {
    return 100;
}
