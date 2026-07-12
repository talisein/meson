#pragma once

#ifdef _CPPUNWIND
inline constexpr bool built_with_eh = true;
#else
inline constexpr bool built_with_eh = false;
#endif
