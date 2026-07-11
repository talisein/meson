import std;
import std.compat;

// The divergent consumer must compile above the provider's dialect: c++26
// over c++23 under clang/gcc, c++latest over c++20 under MSVC (see
// main23.cpp).
#ifdef _MSC_VER
constexpr bool dialect_ok = __cplusplus > 202002L;
#else
constexpr bool dialect_ok = __cplusplus > 202302L;
#endif

int main() {
    if (!dialect_ok) {
        return 1;
    }
    uint32_t v = 42;
    return std::popcount(v) - 3;
}
