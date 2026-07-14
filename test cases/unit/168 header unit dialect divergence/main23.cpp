import "util.h";

int main() {
    // The c++23 class: its own unit BMI must report the c++23 dialect view.
    constexpr int v = util_std_view();
    return v == 23 ? 0 : 1;
}
