export module pkg;

// An interface partition must be exported by its primary interface: importers
// of pkg therefore reach it by definition, not by an implementation's choice.
export import :part;
