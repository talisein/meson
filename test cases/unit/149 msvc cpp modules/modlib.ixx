export module modlib;

export int modfunc() {
    return 42;
}

// Defined in modlib_impl.cpp, a module implementation unit.
export int implfunc();
