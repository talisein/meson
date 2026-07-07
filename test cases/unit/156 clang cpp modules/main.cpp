import modlib;

int main() {
    // modfunc lives in the interface, implfunc in an implementation unit.
    return modfunc() + implfunc() == 62 ? 0 : 1;
}
