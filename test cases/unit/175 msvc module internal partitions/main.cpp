import pkg;

constexpr bool my_view =
#ifdef FOO
    true;
#else
    false;
#endif

int main() {
    return (pkg_with_foo == my_view && pval() == 42) ? 0 : 1;
}
