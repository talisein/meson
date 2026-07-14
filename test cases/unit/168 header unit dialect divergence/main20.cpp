import "util.h";

int main() {
    // Constant-evaluated, so this is the dialect baked into the BMI this TU
    // compiled against: a BMI from the c++23 class is the wrong answer here.
    constexpr int v = util_std_view();
    return v == 20 ? 0 : 1;
}
