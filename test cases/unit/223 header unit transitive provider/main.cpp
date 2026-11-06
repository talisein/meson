import "h.h";
import p;

int main() {
    constexpr int v = h_view();
    return (v == 0 && p_view == 0 && pval() == 42) ? 0 : 1;
}
