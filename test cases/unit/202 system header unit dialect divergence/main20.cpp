import <vector>;

int use_val();

int main() {
    std::vector<int> v{1, 2, 3};
    // main20.cpp compiles under c++20. A c++23 unit BMI leaking into this
    // class's scan would fail the build outright; this exit code catches a
    // subtler dialect confusion in the class this TU resolves. use_val()
    // exercises the sibling TU's <cstdint> unit.
    constexpr bool cpp23 = __cplusplus > 202002L;
    return (v.size() == 3 && !cpp23 && use_val() == 3) ? 0 : 1;
}
