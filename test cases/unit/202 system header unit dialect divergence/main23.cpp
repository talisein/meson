import <vector>;

int main() {
    std::vector<int> v{1, 2, 3};
    // The c++23 class: its own <vector> unit BMI must be the one this TU's
    // scan and compile reach, or the build fails on the dialect mismatch.
    constexpr bool cpp23 = __cplusplus > 202002L;
    return (v.size() == 3 && cpp23) ? 0 : 1;
}
