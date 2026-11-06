import modlib;

// prog never defines FOO: the FOO view it sees is the one modlib's interface
// was preprocessed under, which EXPECT_FOO says. Its dialect, though, must be
// prog's own -- a BMI-only variant's when modlib diverges in cpp_std.
#ifdef EXPECT_FOO
constexpr bool expect_foo = true;
#else
constexpr bool expect_foo = false;
#endif

int main() {
    return (built_with_foo == expect_foo && built_std == __cplusplus) ? (f() - 42) : 1;
}
