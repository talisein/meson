import std.compat;

// This TU imports only std.compat, never std directly. std.compat imports std,
// so the auto-provisioned std must still be built first, transitively
// -- even though nothing here names it.
int main() {
    return std::abs(-7) == 7 ? 0 : 1;
}
