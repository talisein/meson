import std;

// The compatible consumer must really compile at the provider's dialect:
// c++23 under clang/gcc, c++20 under MSVC (the Windows driver runs this
// fixture at -Dcpp_std=c++20 since MSVC has no cpp_std=c++23; __cplusplus is
// truthful there because Meson always passes /Zc:__cplusplus).
#ifdef _MSC_VER
constexpr bool dialect_ok = __cplusplus == 202002L;
#else
constexpr bool dialect_ok = __cplusplus == 202302L;
#endif

int main() {
    if (!dialect_ok) {
        return 1;
    }
    return std::bit_width(32u) - 6;
}
