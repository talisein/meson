import modlib;

int main() {
#if defined(__GXX_RTTI) || defined(_CPPRTTI)
    return 1;
#else
    return mval() == 42 ? 0 : 1;
#endif
}
