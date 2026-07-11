import modlib;

#ifdef _REENTRANT
constexpr bool my_view = true;
#else
constexpr bool my_view = false;
#endif

int main() {
    return my_view == built_with_threads ? (f() - 42) : 1;
}
