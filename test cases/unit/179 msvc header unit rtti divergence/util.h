#pragma once

#ifdef _CPPRTTI
inline constexpr bool built_with_rtti = true;
#else
inline constexpr bool built_with_rtti = false;
#endif
