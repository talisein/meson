import "util.h";

int main() {
    // Same class as prog20: it must reuse prog20's unit BMI, quietly.
    constexpr int v = util_std_view();
    return v == 20 ? 0 : 1;
}
