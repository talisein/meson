export module modlib;

// A runtime-neutral interface: it references no runtime-library symbol, so its
// object links into both a /MD and a /MT executable. A runtime-sensitive
// module (e.g. one exporting STL types, whose ABI tracks _ITERATOR_DEBUG_LEVEL)
// could not share one object across runtimes -- a fundamental MSVC constraint,
// not a module one.
export int modfunc() {
    return 42;
}
