import "util.h";
import modlib;

int main() {
    constexpr int v = util_foo();
    return (v == 0 && mod_util_foo() == 0) ? 0 : 1;
}
