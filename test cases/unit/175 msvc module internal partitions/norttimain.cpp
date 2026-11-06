import pkg;

constexpr bool my_view =
#if defined(__GXX_RTTI) || defined(_CPPRTTI)
    true;
#else
    false;
#endif

int main() {
    return (pkg_with_rtti == my_view && pval() == 42) ? 0 : 1;
}
