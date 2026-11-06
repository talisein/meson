export module pkg;
export import :part;
import :impl;

// Constant-evaluated by every importer: the value comes from :impl's BMI in
// the importer's own class.
export constexpr bool pkg_with_rtti = impl_rtti;

export int pval() {
    return hidden() + part_val();
}
