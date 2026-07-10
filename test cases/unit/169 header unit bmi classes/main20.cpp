import "util.h";
import modlib;

int main() {
    constexpr int v = util_cpp();
    return (v == 20 && mod_util_cpp() == 20) ? 0 : 1;
}
