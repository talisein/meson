import std;
import std.compat;

int main() {
    if (__cplusplus <= 202302L) {
        return 1;
    }
    uint32_t v = 42;
    return std::popcount(v) - 3;
}
