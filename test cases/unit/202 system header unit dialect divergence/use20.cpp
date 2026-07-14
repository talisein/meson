import <cstdint>;

// The target's second system unit, declared by this class alone. The chain
// aliases are per target, so it is still class-alias-named: a scan resolving
// <vector> through the aliased chain resolves <cstdint> through it too, and a
// flat real-named BMI for it would be a path that scan never looks at.
int use_val() {
    std::int32_t v = 3;
    return static_cast<int>(v);
}
