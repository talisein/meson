export module pkg;

export import :part;
import :impl;

export int pkg_value() {
    return part_value() + impl_value() + 1;
}
