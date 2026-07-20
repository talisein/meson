// Ordinary C++, no `export module`: with cpp_modules: false this compiles as
// a normal translation unit under cpp_std=c++17.
#include <cstdio>

static int add(int a, int b) {
    return a + b;
}

int main(void) {
    std::printf("%d\n", add(2, 3));
    return add(2, 3) == 5 ? 0 : 1;
}
