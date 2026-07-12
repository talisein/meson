#pragma once

#ifdef __cpp_exceptions
inline constexpr bool built_with_eh = true;
#else
inline constexpr bool built_with_eh = false;
#endif

#ifdef __cpp_rtti
inline constexpr bool built_with_rtti = true;
#else
inline constexpr bool built_with_rtti = false;
#endif
