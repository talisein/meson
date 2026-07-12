export module pub;

// Public: the plugin that links the executable imports this. constexpr (so
// implicitly inline) deliberately: an entity with module linkage would be
// defined only in the executable's own objects, and exporting *that* across a
// PE/COFF import library needs dllexport gymnastics orthogonal to what this
// fixture is about. The plugin's dependency on the executable's symbols rides
// func_from_app instead (see main.cpp).
export constexpr int pub_value() {
    return 20;
}
