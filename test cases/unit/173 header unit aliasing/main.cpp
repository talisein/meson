import "header.hpp";

int cval();
#ifdef WITH_ALIASED
int aval();
#endif

int main() {
    bool ok = HV == 7 && cval() == 7;
#ifdef WITH_ALIASED
    ok = ok && aval() == 7;
#endif
    return ok ? 0 : 1;
}
