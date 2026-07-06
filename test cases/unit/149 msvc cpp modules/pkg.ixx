export module pkg;

export import :part;

export int pkg_value() {
    return part_value() + 1;
}
