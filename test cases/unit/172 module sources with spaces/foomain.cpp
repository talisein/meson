import modlib;

int main() {
#ifdef FOO
    return mval() == 42 ? 0 : 1;
#else
    return 1;
#endif
}
