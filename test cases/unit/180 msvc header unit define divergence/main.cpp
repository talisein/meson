import "util.h";

#ifdef FOO
constexpr bool my_view = true;
#else
constexpr bool my_view = false;
#endif

int main() {
    return my_view == built_with_foo ? 0 : 1;
}
