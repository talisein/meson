import "h.h";
import p;

// p's and q's BMIs here are variants built against this TU's own h.h, under
// FOO.
int main() {
    constexpr int v = h_view();
    return (v == 1 && p_view == 1 && pval() == 42) ? 0 : 1;
}
