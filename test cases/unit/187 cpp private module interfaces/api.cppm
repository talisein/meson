export module api;

// The interface unit itself imports nothing private: only api_impl.cpp
// (the implementation unit) does, so api's own source stays eligible for a
// cross-class BMI-only variant (see fixture 190) without ever needing a
// private import resolved under foreign class flags.
export int api_value();
