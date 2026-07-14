#pragma once

// util_std_view() folds the dialect it was compiled under into the BMI. Each
// importer constant-evaluates it, so it reads its own class's BMI: a wrongly
// shared BMI would hand back the other dialect's view and a nonzero exit code.
// constexpr, only ever constant-evaluated, so no weak symbol is emitted -- each
// program links just its own class's BMI, but this matches the sibling
// divergence fixtures and never relies on linker de-duplication.
constexpr int util_std_view() {
#if __cplusplus > 202002L
    return 23;
#else
    return 20;
#endif
}
