import modlib;

int main() {
    if (__cplusplus != 202002L) {
        return 1;
    }
    return modfunc() - 42;
}
