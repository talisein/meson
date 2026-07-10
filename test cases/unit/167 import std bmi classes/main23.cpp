import std;

int main() {
    if (__cplusplus != 202302L) {
        return 1;
    }
    return std::bit_width(32u) - 6;
}
